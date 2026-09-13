"""Fake camera fixture for testing gss/media.py without real hardware.

THE SEAM
--------
A real Pi camera integration will:
  1. Start recording at mission launch (when mission.py calls the recording seam).
  2. Write a .mp4 file to RECORDINGS_DIR using the naming convention::

         <mission_id>_<YYYYMMDDTHHMMSS>.mp4

  3. Optionally write a JSON sidecar with position metadata::

         <mission_id>_<YYYYMMDDTHHMMSS>.json

  4. Stop recording when mission.py signals recording stop.

This module replaces that entire camera driver for tests, using ffmpeg's
built-in ``testsrc`` (a synthetic colour-bars pattern with a counter) to
produce short, realistic .mp4 files that ffprobe can read, ffmpeg can
thumbnail, and SHA-256 can hash -- without any real footage or hardware.

See ``tests/fake_vehicle.py`` for the equivalent seam on the MAVLink side.

Library use::

    cam = FakeCamera(recordings_dir="/tmp/rec", ffmpeg_path="ffmpeg")
    path = cam.record("a1b2c3d4-...", duration_s=3.0)
    path2 = cam.record("a1b2c3d4-...", corrupt=True)   # unreadable by ffprobe
    path3, writer = cam.record("b2c3...", still_writing=True)
    time.sleep(2)
    writer.finish()
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class _LiveWriter:
    """Simulates a camera that is still encoding: appends random data to the
    file periodically until ``finish()`` is called or the object is GC'd."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._write_loop, daemon=True, name="fake-cam-writer"
        )
        self._thread.start()

    def _write_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._path.open("ab") as fh:
                    # Append 4 KB of recognisable padding so stat().st_size grows.
                    fh.write(b"\xff\xfe" * 2048)
            except OSError:
                break
            self._stop.wait(0.5)

    def finish(self) -> None:
        """Stop appending. The file is now considered complete."""
        self._stop.set()
        self._thread.join(timeout=3.0)


class FakeCamera:
    """Produces synthetic video files for testing the recording pipeline.

    Each ``record()`` call creates one .mp4 in ``recordings_dir`` using the
    same naming convention that gss/media.py expects from a real camera.
    """

    def __init__(
        self,
        recordings_dir: str | Path,
        *,
        ffmpeg_path: str = "ffmpeg",
    ) -> None:
        self._dir = Path(recordings_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = ffmpeg_path

    # ------------------------------------------------------------------

    def record(
        self,
        mission_id: str,
        *,
        duration_s: float = 3.0,
        width: int = 640,
        height: int = 480,
        corrupt: bool = False,
        still_writing: bool = False,
        lat: float | None = None,
        lon: float | None = None,
        started_at: datetime | None = None,
    ) -> "Path | tuple[Path, _LiveWriter]":
        """Produce a recording file (and optional sidecar) in recordings_dir.

        Parameters
        ----------
        mission_id:
            The mission this recording belongs to.  Written into the filename
            so media.py can extract it without a DB round-trip.
        duration_s:
            Length of the synthetic video (default 3 s -- short for fast tests).
        width, height:
            Frame dimensions.
        corrupt:
            If True, produce a truncated/invalid file that ffprobe cannot read
            (simulates a mid-flight power cut).  Still returns the file path.
        still_writing:
            If True, start a background writer that keeps appending to the file
            so it looks like it is still being encoded.  Returns
            ``(path, LiveWriter)``; call ``writer.finish()`` to stop.
        lat, lon:
            Position at recording start, written into the JSON sidecar.
        started_at:
            Override the recording start timestamp (UTC).  Defaults to now().

        Returns
        -------
        Path if ``still_writing`` is False.
        (Path, _LiveWriter) if ``still_writing`` is True.
        """
        now = started_at or datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%S")
        filename = f"{mission_id}_{ts}.mp4"
        path = self._dir / filename

        if corrupt:
            self._write_corrupt(path)
        elif still_writing:
            # Write a minimal valid file first, then start appending garbage.
            self._write_valid(path, duration_s=max(duration_s, 1.0),
                              width=width, height=height)
            writer = _LiveWriter(path)
            self._write_sidecar(path, mission_id, now, lat, lon)
            return path, writer
        else:
            self._write_valid(path, duration_s=duration_s, width=width, height=height)

        self._write_sidecar(path, mission_id, now, lat, lon)
        return path

    # ------------------------------------------------------------------

    def _write_valid(
        self, path: Path, *, duration_s: float, width: int, height: int
    ) -> None:
        """Produce a playable mp4 using ffmpeg's testsrc source."""
        size = f"{width}x{height}"
        cmd = [
            self._ffmpeg, "-y",
            "-f", "lavfi",
            "-i", f"testsrc=duration={duration_s:.2f}:size={size}:rate=25",
            # Encode with libx264 baseline for maximum compatibility.
            "-c:v", "libx264",
            "-profile:v", "baseline",
            "-level", "3.0",
            "-pix_fmt", "yuv420p",
            # Silence the header/footer to keep test output clean.
            "-loglevel", "error",
            str(path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=True)
        except subprocess.CalledProcessError as exc:
            # If libx264 is not available (some stripped builds), fall back to
            # the very basic mpeg4 codec -- we just need a valid container.
            if b"libx264" in (exc.stderr or b""):
                cmd[cmd.index("libx264")] = "mpeg4"
                cmd.pop(cmd.index("-profile:v"))
                cmd.pop(cmd.index("baseline"))
                cmd.pop(cmd.index("-level"))
                cmd.pop(cmd.index("3.0"))
                subprocess.run(cmd, capture_output=True, timeout=60, check=True)
            else:
                raise

    def _write_corrupt(self, path: Path) -> None:
        """Write a few hundred bytes of garbage -- looks like a truncated file."""
        path.write_bytes(
            b"\x00\x00\x00\x18ftypmp42"  # plausible MP4 header start
            + os.urandom(256)             # random junk
        )

    @staticmethod
    def _write_sidecar(
        path: Path,
        mission_id: str,
        started_at: datetime,
        lat: float | None,
        lon: float | None,
    ) -> None:
        """Write the companion JSON sidecar with position metadata."""
        sidecar = path.with_suffix(".json")
        data: dict = {
            "mission_id": mission_id,
            "started_at": started_at.isoformat(),
        }
        if lat is not None:
            data["lat"] = lat
        if lon is not None:
            data["lon"] = lon
        sidecar.write_text(json.dumps(data), encoding="utf-8")
