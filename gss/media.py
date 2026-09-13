"""Recording pipeline (v0.8) -- file discovery, processing, and retention.

THE SEAM
--------
This module sits between the camera hardware (which does not exist yet) and
the Supabase database (the historical index). When a real Pi camera integration
exists, it writes .mp4 files into RECORDINGS_DIR using the naming convention::

    <mission_id>_<YYYYMMDDTHHMMSS>.mp4

and optionally a sidecar JSON::

    <mission_id>_<YYYYMMDDTHHMMSS>.json

with the mission position at recording start. ``tests/fake_camera.py`` plays
that role in tests -- see its module docstring for the seam description.

RULES
-----
R1  The file-processing core (hash, ffprobe, thumbnail generation) does NOT
    depend on Supabase. Only the final recordings-row INSERT uses the network,
    and it follows store.py's retry pattern on a background thread so a dead
    Supabase never blocks the local pipeline (R10).

R6  Video NEVER goes to Supabase Storage. Only the thumbnail (JPEG, a few
    hundred KB) is uploaded. The local_path column stores the absolute path.

R8  No silent failures. ffmpeg/ffprobe missing → RuntimeError at startup (see
    check_fftools()). A corrupt or unreadable file is flagged (flagged=true in
    the recordings row, detail column carries the reason) rather than silently
    dropped.

R10 A slow or dead Supabase never blocks the scan loop. The INSERT is retried
    inside store.py's own consumer thread; media.py's scanner thread never
    blocks on it.

FILENAME CONVENTION
-------------------
``<mission_id>_<YYYYMMDDTHHMMSS>.mp4``

The mission_id is the first 36 characters of the stem (a UUID). The timestamp
portion makes filenames unique when the same mission somehow produces two files
(rare, but possible if the camera restarts mid-flight). The mission_id is
extracted from the filename alone -- no database round-trip needed.

SETTLE LOGIC
------------
A file is only considered "finished" once its size has been stable for
MEDIA_FILE_SETTLE_S. A file still being written would yield a corrupt hash
and an incorrect duration reading.

RETENTION
---------
A separate thread runs every RETENTION_SCAN_INTERVAL_S. Any recordings row
whose delete_after date has passed has its local file deleted and local_path
cleared (the row itself is kept as a historical index). Files that are already
gone are handled gracefully -- clearing local_path is enough. Rows with
flagged=true are NEVER touched by retention: flagging is a human signal to
preserve the file beyond the normal window.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from gss import config

if TYPE_CHECKING:
    from gss.store import TelemetryStore

log = logging.getLogger(__name__)

# Pattern: <uuid4>_<YYYYMMDDTHHMMSS>[_anything].mp4 (or .mov, .h264 later)
_FILENAME_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"_(\d{8}T\d{6})"
    r"(?:_.*)?"
    r"\.(mp4|mov|h264)$",
    re.IGNORECASE,
)

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".h264"}

# How many 1 MB chunks to read when hashing (avoids loading a 4 GB file at once).
_HASH_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
# Startup check -- called before any MediaScanner is constructed
# ---------------------------------------------------------------------------


def check_fftools(
    ffmpeg_path: str = "",
    ffprobe_path: str = "",
) -> None:
    """Verify that ffmpeg and ffprobe are reachable.

    Raises ``RuntimeError`` with a clear message if either is missing (R8).
    ``ffmpeg_path`` / ``ffprobe_path`` default to the config values.
    Called by ``MediaScanner.__init__`` and also importable for tests.
    """
    fmpeg = ffmpeg_path or config.FFMPEG_PATH
    fprobe = ffprobe_path or config.FFPROBE_PATH
    missing = []
    for name, path in (("ffmpeg", fmpeg), ("ffprobe", fprobe)):
        if not shutil.which(path):
            missing.append(
                f"{name!r} not found at {path!r}. "
                f"Install ffmpeg (e.g. sudo apt-get install ffmpeg on the Pi) "
                f"or set {name.upper()}_PATH in .env. "
                f"Set MEDIA_ENABLED=false to skip media processing."
            )
    if missing:
        raise RuntimeError(
            "media.py cannot start -- missing required tool(s):\n  - "
            + "\n  - ".join(missing)
        )


# ---------------------------------------------------------------------------
# Low-level helpers (pure local I/O, never touch Supabase)
# ---------------------------------------------------------------------------


def _ffprobe(path: Path, ffprobe_path: str = "") -> dict:
    """Run ffprobe and return {duration_s, width, height, size_mb}.

    Raises ``subprocess.CalledProcessError`` or ``ValueError`` on any failure.
    """
    exe = ffprobe_path or config.FFPROBE_PATH
    cmd = [
        exe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    result = subprocess.run(
        cmd, capture_output=True, timeout=30, check=True
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe returned unparseable JSON for {path.name}: {exc}") from exc

    # Duration from format (most reliable).
    try:
        duration_s = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"ffprobe: no duration for {path.name}") from exc

    # Width/height from the first video stream.
    width = height = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = stream.get("width")
            height = stream.get("height")
            break

    size_mb = path.stat().st_size / (1024 * 1024)

    return {
        "duration_s": duration_s,
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "size_mb": round(size_mb, 3),
    }


def _sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file (streaming, 1 MB chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _make_thumbnail(src: Path, dst: Path, ffmpeg_path: str = "") -> None:
    """Extract the first video frame from ``src`` and write it as a JPEG to ``dst``.

    Raises ``subprocess.CalledProcessError`` on failure.
    """
    exe = ffmpeg_path or config.FFMPEG_PATH
    cmd = [
        exe, "-y",               # overwrite dst if it exists
        "-i", str(src),
        "-frames:v", "1",        # one frame only
        "-q:v", "3",             # JPEG quality (1=best, 31=worst; 3 ≈ 300 KB)
        "-vf", "scale=640:-1",   # shrink to 640-wide (keeps AR)
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, timeout=30, check=True)


def _extract_mission_id(path: Path) -> str | None:
    """Parse the mission_id from a recording filename, or return None."""
    m = _FILENAME_RE.match(path.name)
    if m:
        return m.group(1)
    return None


def _extract_started_at(path: Path) -> str | None:
    """Parse the started_at timestamp from the filename (UTC, ISO-8601)."""
    m = _FILENAME_RE.match(path.name)
    if not m:
        return None
    ts = m.group(2)  # YYYYMMDDTHHMMSS
    try:
        dt = datetime.strptime(ts, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def _read_sidecar(path: Path) -> dict:
    """Read the JSON sidecar (same stem, .json extension).

    Returns {} if the sidecar is absent or unparseable. A missing sidecar is
    not an error -- a real camera may not write one; lat/lon will be None.
    """
    sidecar = path.with_suffix(".json")
    if not sidecar.exists():
        return {}
    try:
        with sidecar.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("media: sidecar %s unreadable (%s); ignoring", sidecar.name, exc)
        return {}


def _is_settled(path: Path, settle_s: float) -> bool:
    """Return True if the file's size has not changed in ``settle_s`` seconds.

    Uses two stat() calls ``settle_s`` apart. Safe to call from any thread.
    A file that does not exist yet returns False (the caller should skip it).
    """
    try:
        size_before = path.stat().st_size
    except OSError:
        return False
    time.sleep(settle_s)
    try:
        size_after = path.stat().st_size
    except OSError:
        return False
    return size_after == size_before


# ---------------------------------------------------------------------------
# Per-file processor
# ---------------------------------------------------------------------------


class MediaProcessor:
    """Stateless processor for a single recording file.

    All heavy lifting (ffprobe, hash, thumbnail) runs locally (R1).  Only the
    final ``create_recording`` call touches the network. If that fails, the
    caller retries on the next scan pass.
    """

    def __init__(
        self,
        store: "TelemetryStore",
        *,
        drone_id: str | None = None,
        thumbnail_bucket: str | None = None,
        ffmpeg_path: str = "",
        ffprobe_path: str = "",
        keep_days: int | None = None,
    ) -> None:
        self._store = store
        self._drone_id = drone_id or config.DRONE_ID
        self._bucket = thumbnail_bucket or config.RECORDINGS_THUMBNAIL_BUCKET
        self._ffmpeg = ffmpeg_path or config.FFMPEG_PATH
        self._ffprobe = ffprobe_path or config.FFPROBE_PATH
        self._keep_days = keep_days if keep_days is not None else config.RECORDING_KEEP_DAYS

    def process(self, path: Path) -> bool:
        """Process one recording file end-to-end.

        Returns True if the recordings row was successfully created (or already
        existed). Returns False if the file should be retried on the next scan
        pass. On a hard failure (corrupt file, ffprobe error) the file is
        flagged rather than lost and False is returned (retry on next pass to
        re-attempt the DB write).
        """
        log.info("media: processing %s", path.name)

        mission_id = _extract_mission_id(path)
        started_at = _extract_started_at(path)
        sidecar = _read_sidecar(path)
        lat = sidecar.get("lat")
        lon = sidecar.get("lon")
        if started_at is None:
            started_at = sidecar.get("started_at")

        # ---- 1. Compute SHA-256 (local, R1) --------------------------------
        try:
            sha = _sha256(path)
        except OSError as exc:
            log.error("media: cannot read %s for hashing: %s", path.name, exc)
            return False

        # ---- idempotency: have we already processed this exact file? --------
        existing = self._store.get_recording_by_sha256(sha, self._drone_id)
        if existing is not None:
            log.info(
                "media: %s already indexed (id=%s); skipping",
                path.name, existing.get("id", "?"),
            )
            return True

        # ---- 2. ffprobe (local, R1) ----------------------------------------
        ffprobe_info: dict = {}
        error_reason: str | None = None
        flagged = False

        try:
            ffprobe_info = _ffprobe(path, self._ffprobe)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                ValueError, OSError) as exc:
            error_reason = f"ffprobe failed: {exc}"
            flagged = True
            log.error("media: %s flagged -- %s", path.name, error_reason)

        # ---- 3. Thumbnail (local generation, then network upload) -----------
        thumbnail_url: str | None = None
        tmp_thumb: tempfile.NamedTemporaryFile | None = None

        if not flagged:
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".jpg", delete=False
                ) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    _make_thumbnail(path, tmp_path, self._ffmpeg)
                    thumb_bytes = tmp_path.read_bytes()
                    # object path inside the bucket: drone_id/mission_id/filename.jpg
                    mid_part = mission_id or "unknown"
                    obj_path = f"{self._drone_id}/{mid_part}/{path.stem}.jpg"
                    thumbnail_url = self._store.upload_to_storage(
                        self._bucket, obj_path, thumb_bytes,
                    )
                    log.info("media: thumbnail uploaded -> %s", thumbnail_url)
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    OSError, Exception) as exc:  # noqa: BLE001
                # Thumbnail failure is not a hard block: the video and hash
                # are still valid evidence. Flag it and continue writing the row.
                error_reason = f"thumbnail failed: {type(exc).__name__}: {exc}"
                flagged = True
                log.error(
                    "media: %s thumbnail step failed -- flagging: %s",
                    path.name, error_reason,
                )

        # ---- 4. Write recordings row (network, R10: runs on THIS thread) ----
        delete_after = (
            date.fromordinal(
                date.today().toordinal() + self._keep_days
            ).isoformat()
            if not flagged else None
        )
        detail_payload = {"error": error_reason} if error_reason else None

        row_id = self._store.create_recording(
            drone_id=self._drone_id,
            mission_id=mission_id,
            local_path=str(path.resolve()),
            started_at=started_at,
            duration_s=ffprobe_info.get("duration_s"),
            size_mb=ffprobe_info.get("size_mb"),
            thumbnail_url=thumbnail_url,
            sha256=sha,
            media_type="video",
            width=ffprobe_info.get("width"),
            height=ffprobe_info.get("height"),
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            flagged=flagged,
            detail=detail_payload,
            delete_after=delete_after,
        )
        if row_id is None:
            log.error(
                "media: could not write recordings row for %s; will retry",
                path.name,
            )
            return False

        if flagged:
            log.warning(
                "media: %s flagged (id=%s) -- reason: %s. "
                "File left untouched. Human review required before retention will touch it.",
                path.name, row_id, error_reason,
            )
        else:
            log.info(
                "media: %s indexed (id=%s, dur=%.1fs, %.0fx%.0f, %.2f MB, sha256=%s...)",
                path.name, row_id,
                ffprobe_info.get("duration_s", 0),
                ffprobe_info.get("width") or 0, ffprobe_info.get("height") or 0,
                ffprobe_info.get("size_mb", 0), sha[:12],
            )
        return True


# ---------------------------------------------------------------------------
# MediaScanner -- the long-running service
# ---------------------------------------------------------------------------


class MediaScanner:
    """Watches RECORDINGS_DIR for finished recordings and runs the pipeline.

    Two threads:
      * **scan loop** -- wakes every MEDIA_SCAN_INTERVAL_S, finds new files,
        waits for them to settle, processes them.
      * **retention loop** -- wakes every RETENTION_SCAN_INTERVAL_S, deletes
        expired files, clears local_path (keeps the row as a historical index).

    No camera hardware is required; ``tests/fake_camera.py`` produces synthetic
    files that exercise the full pipeline.
    """

    def __init__(
        self,
        store: "TelemetryStore",
        *,
        drone_id: str | None = None,
        recordings_dir: str | None = None,
        settle_s: float | None = None,
        scan_interval_s: float | None = None,
        retention_interval_s: float | None = None,
        thumbnail_bucket: str | None = None,
        ffmpeg_path: str = "",
        ffprobe_path: str = "",
        keep_days: int | None = None,
        skip_fftools_check: bool = False,
    ) -> None:
        if not skip_fftools_check:
            check_fftools(ffmpeg_path, ffprobe_path)

        self._store = store
        self._drone_id = drone_id or config.DRONE_ID
        self._recordings_dir = Path(recordings_dir or config.RECORDINGS_DIR)
        self._settle_s = settle_s if settle_s is not None else config.MEDIA_FILE_SETTLE_S
        self._scan_interval_s = (
            scan_interval_s if scan_interval_s is not None else config.MEDIA_SCAN_INTERVAL_S
        )
        self._retention_interval_s = (
            retention_interval_s
            if retention_interval_s is not None
            else config.RETENTION_SCAN_INTERVAL_S
        )
        self._processor = MediaProcessor(
            store,
            drone_id=self._drone_id,
            thumbnail_bucket=thumbnail_bucket,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            keep_days=keep_days,
        )

        self._stop = threading.Event()
        self._processed: set[Path] = set()  # in-memory, rebuilt on restart
        self._processed_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._retention_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the scan and retention threads (idempotent)."""
        if self._scan_thread is not None:
            return
        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "media: scanner starting (dir=%s, settle=%.1fs, scan=%.0fs, retain=%.0fs)",
            self._recordings_dir, self._settle_s,
            self._scan_interval_s, self._retention_interval_s,
        )
        self._scan_thread = threading.Thread(
            target=self._scan_loop, name="media-scan", daemon=True
        )
        self._retention_thread = threading.Thread(
            target=self._retention_loop, name="media-retain", daemon=True
        )
        self._scan_thread.start()
        self._retention_thread.start()

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop both threads gracefully."""
        self._stop.set()
        for thread in (self._scan_thread, self._retention_thread):
            if thread is not None:
                thread.join(timeout=timeout_s)
        log.info("media: scanner stopped")

    # ---------------------------------------------------------------- scan loop

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception:  # noqa: BLE001 -- R8: never let a scan crash the thread
                log.exception("media: scan cycle failed")
            self._stop.wait(self._scan_interval_s)

    def _scan_once(self) -> None:
        """One scan pass: find new video files, settle-check, process."""
        if not self._recordings_dir.exists():
            return
        for path in sorted(self._recordings_dir.iterdir()):
            if self._stop.is_set():
                return
            if path.suffix.lower() not in _VIDEO_EXTENSIONS:
                continue
            with self._processed_lock:
                if path in self._processed:
                    continue

            # Check settle: if the file is still growing, skip and try next pass.
            if not _is_settled(path, self._settle_s):
                log.debug(
                    "media: %s still growing after %.1fs -- deferring",
                    path.name, self._settle_s,
                )
                continue

            # Attempt to process. On success (True) mark as done; on failure
            # (False) leave out of the set so we retry on the next pass.
            ok = False
            try:
                ok = self._processor.process(path)
            except Exception:  # noqa: BLE001 -- R8: log it, never silently swallow
                log.exception("media: unexpected error processing %s", path.name)

            if ok:
                with self._processed_lock:
                    self._processed.add(path)

    # ------------------------------------------------------------- retention loop

    def _retention_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._retention_once()
            except Exception:  # noqa: BLE001 -- R8
                log.exception("media: retention cycle failed")
            self._stop.wait(self._retention_interval_s)

    def _retention_once(self) -> None:
        """One retention pass: delete expired files and clear local_path."""
        today = date.today().isoformat()
        rows = self._store.list_expired_recordings(self._drone_id, today)
        if not rows:
            return
        log.info("media: retention pass -- %d expired recording(s) to clean", len(rows))
        for row in rows:
            rec_id = row["id"]
            local_path_str = row.get("local_path")
            if local_path_str:
                p = Path(local_path_str)
                if p.exists():
                    try:
                        p.unlink()
                        log.info("media: deleted expired recording %s", p.name)
                    except OSError as exc:
                        log.error(
                            "media: could not delete %s: %s -- clearing local_path anyway",
                            p, exc,
                        )
                else:
                    log.info(
                        "media: expired recording %s already gone -- clearing local_path",
                        p.name,
                    )
            # Clear local_path regardless of whether delete succeeded -- the
            # historical index row is preserved, just without a file reference.
            if not self._store.update_recording(rec_id, local_path=None):
                log.error(
                    "media: could not clear local_path for recording %s; "
                    "will retry on the next retention pass",
                    rec_id,
                )
