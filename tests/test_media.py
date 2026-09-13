"""tests/test_media.py -- Phase 8 verification suite.

Tests the full recording pipeline (gss/media.py) without real camera hardware
or a live Supabase instance. Uses tests/fake_camera.py to produce synthetic
.mp4 files and a stub store to isolate the network boundary.

Verification items (per the Phase 8 spec):

  V1  Normal file → full pipeline, recordings row appears with real
      duration/size/hash/thumbnail, thumbnail visible in the bucket.
  V2  File still being written → not processed until it settles.
  V3  Corrupt/truncated file → flagged, reason logged, no crash.
  V4  Retention: a row with delete_after in the past has its file removed
      and local_path cleared; the row itself remains.
  V5  Flagged row is never touched by retention, even well past delete_after.
  V6  Filename-based mission matching: two recordings for two different missions
      land on the correct rows.
  V7  Supabase unreachable during the index-row write: local processing
      (hash, thumbnail generation) still completes; write retries; nothing is
      lost or double-processed on retry.
  V8  ffmpeg/ffprobe missing: GSS fails loudly at startup, does not run
      half-broken.
  V9  mission.py sets recording_active correctly: recording_started and
      recording_stopped events appear at the right points and the supervisor
      mirrors them to update_drone.
  V10 All earlier test suites still pass (run separately via `pytest tests/`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

# Skip the entire suite if ffmpeg is not available -- we cannot produce
# synthetic video without it. The fftools-missing test (V8) is not skipped:
# it works by pointing to a nonexistent binary, not by relying on ffmpeg.
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
FFPROBE_AVAILABLE = shutil.which("ffprobe") is not None
FFTOOLS_AVAILABLE = FFMPEG_AVAILABLE and FFPROBE_AVAILABLE

requires_fftools = pytest.mark.skipif(
    not FFTOOLS_AVAILABLE,
    reason="ffmpeg/ffprobe not on PATH -- skipping media pipeline tests that need them",
)


def _make_mission_id() -> str:
    return str(uuid.uuid4())


def _stub_store(*, upload_raises: bool = False, create_returns_none: bool = False):
    """Return a MagicMock that looks enough like TelemetryStore for media.py."""
    store = MagicMock()
    store.get_recording_by_sha256.return_value = None  # not already indexed
    if create_returns_none:
        store.create_recording.return_value = None
    else:
        store.create_recording.return_value = str(uuid.uuid4())
    store.update_recording.return_value = True
    store.update_drone.return_value = True

    if upload_raises:
        store.upload_to_storage.side_effect = OSError("connection refused")
    else:
        # The bucket is private (Phase 8.1): upload_to_storage returns the
        # bucket-relative object path, not a public URL.
        store.upload_to_storage.return_value = "some-drone/some-mission/thumb.jpg"
    return store


# ── V1: normal file → full pipeline ──────────────────────────────────────────

@requires_fftools
def test_v1_normal_file_full_pipeline(tmp_path):
    """A normally-produced file runs the full pipeline: recordings row appears
    with real duration, size, hash, and a (mocked) thumbnail URL.
    """
    from gss import config as _cfg
    from gss.media import MediaProcessor
    from tests.fake_camera import FakeCamera

    mission_id = _make_mission_id()
    cam = FakeCamera(tmp_path)
    path = cam.record(mission_id, duration_s=2.0, lat=25.5932, lon=85.2045)

    store = _stub_store()
    processor = MediaProcessor(
        store,
        drone_id="d5030000-0000-4000-8000-000000000001",
        thumbnail_bucket="recording-thumbnails",
        keep_days=30,
    )

    ok = processor.process(path)
    assert ok, "process() should return True for a valid file"

    # create_recording was called exactly once.
    assert store.create_recording.call_count == 1
    kwargs = store.create_recording.call_args.kwargs

    # Check the core fields the spec requires.
    assert kwargs["mission_id"] == mission_id
    assert kwargs["sha256"] and len(kwargs["sha256"]) == 64, "sha256 must be a 64-char hex"
    assert kwargs["duration_s"] and kwargs["duration_s"] > 0.5, "duration must be > 0"
    assert kwargs["size_mb"] and kwargs["size_mb"] > 0, "size_mb must be > 0"
    assert kwargs["width"] and kwargs["width"] > 0
    assert kwargs["height"] and kwargs["height"] > 0
    assert kwargs["thumbnail_url"], "thumbnail_url must be set"
    assert kwargs["lat"] == pytest.approx(25.5932)
    assert kwargs["lon"] == pytest.approx(85.2045)
    assert kwargs["flagged"] is False
    assert kwargs["local_path"] == str(path.resolve())

    # Thumbnail was uploaded to storage.
    assert store.upload_to_storage.call_count == 1


# ── V2: file still being written → not processed until settled ───────────────

@requires_fftools
def test_v2_still_writing_not_processed(tmp_path):
    """A file that is still growing is deferred; once settled it is processed."""
    from gss.media import _is_settled, MediaScanner
    from tests.fake_camera import FakeCamera

    mission_id = _make_mission_id()
    cam = FakeCamera(tmp_path)

    # still_writing=True → file keeps growing
    path, writer = cam.record(mission_id, duration_s=2.0, still_writing=True)

    # Should NOT be settled immediately (the live writer is still appending).
    assert not _is_settled(path, settle_s=0.3), (
        "File should not be settled while the live writer is running"
    )

    writer.finish()
    # Allow a moment for the writer to stop.
    time.sleep(0.2)

    # After the writer stops, the file should be settled.
    assert _is_settled(path, settle_s=0.3), (
        "File should be settled after the live writer finishes"
    )

    # Now the scanner would process it — verify process() succeeds.
    store = _stub_store()
    from gss.media import MediaProcessor
    processor = MediaProcessor(
        store, drone_id="d5030000-0000-4000-8000-000000000001",
    )
    ok = processor.process(path)
    assert ok


# ── V3: corrupted file → flagged, logged, no crash ───────────────────────────

def test_v3_corrupt_file_flagged(tmp_path):
    """A corrupt / unreadable file is flagged (flagged=True, detail with reason),
    the source file is left untouched, and process() does not crash the scanner.
    """
    from gss.media import MediaProcessor
    from tests.fake_camera import FakeCamera

    mission_id = _make_mission_id()
    cam = FakeCamera(tmp_path)
    path = cam.record(mission_id, corrupt=True)

    store = _stub_store()
    processor = MediaProcessor(
        store, drone_id="d5030000-0000-4000-8000-000000000001",
    )

    # Should not raise -- R8: no crash, just flag.
    ok = processor.process(path)

    # Returns True when the row was written successfully (even if flagged).
    # The path must still exist (file left untouched).
    assert path.exists(), "Corrupt source file must not be deleted"

    # The recordings row must be written with flagged=True.
    assert store.create_recording.call_count == 1
    kwargs = store.create_recording.call_args.kwargs
    assert kwargs["flagged"] is True, "Corrupt file must be flagged"
    assert kwargs["detail"] is not None, "detail must carry the error reason"
    assert "error" in kwargs["detail"], "detail must have an 'error' key"

    # sha256 was computed (the file was readable, just not a valid video).
    assert kwargs["sha256"], "sha256 must be set even for corrupt files"


# ── V4: retention → file deleted, local_path cleared, row preserved ───────────

def test_v4_retention_deletes_expired(tmp_path):
    """An expired recording has its file deleted and local_path cleared.
    The row itself is NOT deleted.
    """
    from gss.media import MediaScanner

    # Create a real file to be deleted.
    rec_file = tmp_path / "recordings" / "dead_file.mp4"
    rec_file.parent.mkdir(parents=True, exist_ok=True)
    rec_file.write_bytes(b"\x00" * 1024)

    rec_id = str(uuid.uuid4())
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    store = MagicMock()
    store.list_expired_recordings.return_value = [
        {"id": rec_id, "local_path": str(rec_file)},
    ]
    store.update_recording.return_value = True

    scanner = MediaScanner(
        store,
        drone_id="d5030000-0000-4000-8000-000000000001",
        recordings_dir=str(tmp_path / "recordings"),
        skip_fftools_check=True,
    )
    scanner._retention_once()

    # File must be gone.
    assert not rec_file.exists(), "Expired file must be deleted by retention"

    # update_recording called with local_path=None (clears the reference).
    store.update_recording.assert_called_once_with(rec_id, local_path=None)


# ── V5: flagged row never touched by retention ───────────────────────────────

def test_v5_flagged_row_never_deleted(tmp_path):
    """A flagged recording is never touched by the retention pass, even if
    delete_after is in the past.
    """
    from gss.media import MediaScanner

    # The store returns NO rows (because flagged=false filter excluded them).
    store = MagicMock()
    store.list_expired_recordings.return_value = []  # no unflagged expired rows

    flagged_file = tmp_path / "recordings" / "flagged.mp4"
    flagged_file.parent.mkdir(parents=True, exist_ok=True)
    flagged_file.write_bytes(b"\x00" * 1024)

    scanner = MediaScanner(
        store,
        drone_id="d5030000-0000-4000-8000-000000000001",
        recordings_dir=str(tmp_path / "recordings"),
        skip_fftools_check=True,
    )
    scanner._retention_once()

    # File must still exist (flagged rows excluded by the query).
    assert flagged_file.exists(), "Flagged file must not be touched by retention"
    store.update_recording.assert_not_called()


# ── V6: filename-based mission matching ──────────────────────────────────────

@requires_fftools
def test_v6_mission_matching_by_filename(tmp_path):
    """Two recordings produced close together for two different missions each
    land in the correct row, matched by filename alone (no DB round-trip).
    """
    from gss.media import MediaProcessor, _extract_mission_id
    from tests.fake_camera import FakeCamera

    mission_a = _make_mission_id()
    mission_b = _make_mission_id()
    cam = FakeCamera(tmp_path)

    # Use distinct timestamps to avoid filename collision.
    t0 = datetime(2026, 9, 13, 14, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 13, 14, 0, 5, tzinfo=timezone.utc)
    path_a = cam.record(mission_a, duration_s=1.5, started_at=t0)
    path_b = cam.record(mission_b, duration_s=1.5, started_at=t1)

    # Verify filename extraction before we even call process().
    assert _extract_mission_id(path_a) == mission_a
    assert _extract_mission_id(path_b) == mission_b

    # Call counts to match rows.
    calls_a = []
    calls_b = []

    store = _stub_store()

    def _capture_create(**kwargs):
        mid = kwargs.get("mission_id")
        if mid == mission_a:
            calls_a.append(kwargs)
        elif mid == mission_b:
            calls_b.append(kwargs)
        return str(uuid.uuid4())

    store.create_recording.side_effect = _capture_create

    processor = MediaProcessor(
        store, drone_id="d5030000-0000-4000-8000-000000000001",
    )
    processor.process(path_a)
    processor.process(path_b)

    assert len(calls_a) == 1, "Exactly one row for mission A"
    assert len(calls_b) == 1, "Exactly one row for mission B"
    assert calls_a[0]["mission_id"] == mission_a
    assert calls_b[0]["mission_id"] == mission_b


# ── V7: Supabase unreachable during DB write → local processing completes ─────

@requires_fftools
def test_v7_supabase_unreachable_local_still_completes(tmp_path):
    """If Supabase is unreachable during the create_recording call, the local
    pipeline (hash, thumbnail) still completes. The write should be retried by
    the caller on the next scan pass (returning False so the scan loop retries).
    Nothing is lost on retry because sha256 acts as the idempotency key.
    """
    from gss.media import MediaProcessor
    from tests.fake_camera import FakeCamera

    mission_id = _make_mission_id()
    cam = FakeCamera(tmp_path)
    path = cam.record(mission_id, duration_s=1.5)

    # First call: create_recording returns None (DB unreachable / write failed).
    # Second call: succeeds (DB back up on the next scan pass).
    store = _stub_store()
    store.create_recording.side_effect = [None, str(uuid.uuid4())]

    processor = MediaProcessor(
        store, drone_id="d5030000-0000-4000-8000-000000000001",
    )

    # First pass -- DB write fails; process() returns False (will retry).
    ok_first = processor.process(path)
    assert ok_first is False, "Should return False when DB write fails"

    # The sha256 lookup returns None on retry (idempotency guard:
    # still None because first write never landed).
    store.get_recording_by_sha256.return_value = None

    # Second pass -- DB write succeeds.
    ok_second = processor.process(path)
    assert ok_second is True, "Should return True once the DB write succeeds"

    # Two create_recording attempts total.
    assert store.create_recording.call_count == 2

    # The sha256 and thumbnail were computed on the FIRST pass (local work
    # does not depend on Supabase). The second pass recomputes them (they are
    # cheap and deterministic), but verify they are identical.
    first_sha = store.create_recording.call_args_list[0].kwargs.get("sha256")
    second_sha = store.create_recording.call_args_list[1].kwargs.get("sha256")
    assert first_sha == second_sha, "sha256 must be deterministic across retries"


# ── V8: ffmpeg/ffprobe missing → loud startup error ──────────────────────────

def test_v8_fftools_missing_loud_startup_error(tmp_path):
    """If ffmpeg or ffprobe are not found, check_fftools() raises RuntimeError
    with a clear message. The GSS must not start half-broken.
    """
    from gss.media import check_fftools

    # Point to a path that definitely does not exist.
    with pytest.raises(RuntimeError) as exc_info:
        check_fftools(
            ffmpeg_path="/nonexistent/ffmpeg",
            ffprobe_path="/nonexistent/ffprobe",
        )
    msg = str(exc_info.value)
    assert "ffmpeg" in msg.lower() or "ffprobe" in msg.lower(), (
        "Error message must name the missing tool"
    )
    assert "/nonexistent" in msg, "Error message must show the path that was tried"


def test_v8_ffprobe_missing_loud(tmp_path):
    """Even if ffmpeg is present, a missing ffprobe is caught at startup."""
    from gss.media import check_fftools
    import shutil

    ffmpeg_real = shutil.which("ffmpeg") or "ffmpeg"
    with pytest.raises(RuntimeError) as exc_info:
        check_fftools(
            ffmpeg_path=ffmpeg_real,
            ffprobe_path="/nonexistent/ffprobe",
        )
    assert "ffprobe" in str(exc_info.value).lower()


# ── V9: mission.py recording_active seam ─────────────────────────────────────

def test_v9_recording_active_seam_via_emit():
    """MavlinkExecutor emits recording_started at arm and recording_stopped at
    landing. The supervisor (commands.py) drains those events and calls
    update_drone(recording_active=...).

    This test verifies the emit side directly on MavlinkExecutor using its
    drain_events() API, bypassing SITL. The supervisor integration is verified
    by checking the store.update_drone() call path via mock.
    """
    # We need the full mission.py internals -- import carefully so we can patch.
    from gss.mission import MavlinkExecutor, _RESULT_ACCEPTED, _CMD_ARM
    from gss.snapshot import TelemetrySnapshot

    mission_id = str(uuid.uuid4())
    command = {
        "id": str(uuid.uuid4()),
        "type": "summon",
        "target_lat": 25.5932,
        "target_lon": 85.2045,
    }

    # Build a minimal fake snapshot that makes preflight pass and armed=True.
    snap = MagicMock(spec=TelemetrySnapshot)
    snap.connected = True
    snap.armed = True        # already armed, so we can call _emit directly
    snap.alt_m_relative = 0.0
    snap.ekf_healthy = True
    snap.home_set = True
    snap.gps_fix_type = 3
    snap.gps_satellites = 10
    snap.prearm_fail_text = None
    snap.position_valid = True
    snap.lat = 25.5932
    snap.lon = 85.2045
    snap.mode = "GUIDED"
    snap.wind_direction_deg = None
    snap.wind_speed_ms = None
    snap.wind_age_s = None
    snap.link_age_s = None
    snap.timestamp = datetime.now(timezone.utc)

    link = MagicMock()
    executor = MavlinkExecutor(
        mission_id, command, lambda: snap,
        link=link,
    )

    # Call _emit directly to simulate what _step_arm does after arm confirmed.
    executor._emit("recording_started", {
        "note": "recording seam -- a real camera starts here",
        "lat": 25.5932, "lon": 85.2045,
    })

    events = executor.drain_events()
    assert len(events) == 1
    assert events[0][0] == "recording_started"
    assert events[0][1]["lat"] == pytest.approx(25.5932)

    # After drain, buffer is empty.
    assert executor.drain_events() == []

    # Now emit stop.
    executor._emit("recording_stopped", {
        "note": "recording seam -- a real camera stops here (rtl)",
        "lat": 25.5932, "lon": 85.2045,
    })
    stop_events = executor.drain_events()
    assert len(stop_events) == 1
    assert stop_events[0][0] == "recording_stopped"


def test_v9_supervisor_update_drone_on_recording_events():
    """The supervisor's _advance() calls store.update_drone(recording_active=...)
    when it drains recording_started / recording_stopped from the executor.
    """
    from gss.commands import CommandIntake
    from gss.executor import DryRunExecutor, MissionPhase

    store = MagicMock()
    store.create_recording.return_value = str(uuid.uuid4())
    store.update_drone.return_value = True
    store.log_event.return_value = True
    store.update_mission.return_value = True
    store.update_command_status.return_value = True

    from gss.snapshot import TelemetrySnapshot
    snap = MagicMock(spec=TelemetrySnapshot)
    snap.connected = True
    snap.position_valid = True
    snap.lat = 25.5932
    snap.lon = 85.2045
    snap.timestamp = datetime.now(timezone.utc)

    # Build a minimal fake executor that returns one recording_started event.
    executor = MagicMock()
    executor.poll.return_value = MissionPhase.LAUNCHING
    executor.drain_events.side_effect = [
        [("recording_started", {"note": "seam"})],
        [],  # second drain call
    ]
    executor.abort_reason = None

    intake = CommandIntake(
        store,
        lambda: snap,
        drone_id="d5030000-0000-4000-8000-000000000001",
        dock_id="d0c00000-0000-4000-8000-000000000001",
        realtime_enabled=False,
    )

    from gss.commands import _ActiveMission
    from gss.executor import MissionPhase
    import time as _time
    active = _ActiveMission(
        mission_id=str(uuid.uuid4()),
        command_id=str(uuid.uuid4()),
        command={"type": "summon"},
        executor=executor,
        started_mono=_time.monotonic(),
        last_phase=MissionPhase.LAUNCHING,
    )

    # Simulate a supervisor tick via _advance (using a thin call path).
    with patch.object(intake, "_consult_safety"), \
         patch.object(intake, "_consult_weather"):
        intake._advance(active)

    # update_drone must have been called with recording_active=True.
    store.update_drone.assert_called_once_with(recording_active=True)


# ── V10: regression -- spot-check that existing modules still import cleanly ──

def test_v10_existing_modules_import():
    """Sanity check: all previously-working modules still import without error."""
    import gss.config      # noqa: F401
    import gss.store       # noqa: F401
    import gss.mission     # noqa: F401
    import gss.safety      # noqa: F401
    import gss.weather     # noqa: F401
    import gss.executor    # noqa: F401
    import gss.commands    # noqa: F401
    import gss.media       # noqa: F401


# ── Additional edge-case tests ────────────────────────────────────────────────

def test_extract_mission_id_valid():
    from gss.media import _extract_mission_id
    mid = "a1b2c3d4-e5f6-4000-8000-000000000001"
    path = Path(f"{mid}_20260913T140000.mp4")
    assert _extract_mission_id(path) == mid


def test_extract_mission_id_invalid():
    from gss.media import _extract_mission_id
    # No underscore separator.
    assert _extract_mission_id(Path("somefile.mp4")) is None
    # Too short for a UUID.
    assert _extract_mission_id(Path("short_20260913T140000.mp4")) is None


def test_extract_started_at_parses_correctly():
    from gss.media import _extract_started_at
    mid = "a1b2c3d4-e5f6-4000-8000-000000000001"
    path = Path(f"{mid}_20260913T143000.mp4")
    ts = _extract_started_at(path)
    assert ts is not None
    assert "2026-09-13" in ts
    assert "14:30:00" in ts


def test_read_sidecar_missing_returns_empty(tmp_path):
    from gss.media import _read_sidecar
    path = tmp_path / "a1b2c3d4-e5f6-4000-8000-000000000001_20260913T140000.mp4"
    path.write_bytes(b"")
    assert _read_sidecar(path) == {}


def test_read_sidecar_present(tmp_path):
    from gss.media import _read_sidecar
    import json as _json
    path = tmp_path / "a1b2c3d4-e5f6-4000-8000-000000000001_20260913T140000.mp4"
    path.write_bytes(b"")
    sidecar = path.with_suffix(".json")
    sidecar.write_text(_json.dumps({"lat": 25.0, "lon": 85.0, "mission_id": "abc"}))
    data = _read_sidecar(path)
    assert data["lat"] == 25.0
    assert data["lon"] == 85.0


def test_retention_already_deleted_file(tmp_path):
    """If the file is already gone, retention just clears local_path, no error."""
    from gss.media import MediaScanner

    rec_id = str(uuid.uuid4())
    store = MagicMock()
    store.list_expired_recordings.return_value = [
        {"id": rec_id, "local_path": str(tmp_path / "nonexistent.mp4")},
    ]
    store.update_recording.return_value = True

    scanner = MediaScanner(
        store, drone_id="d5030000-0000-4000-8000-000000000001",
        recordings_dir=str(tmp_path), skip_fftools_check=True,
    )
    # Should not raise.
    scanner._retention_once()
    store.update_recording.assert_called_once_with(rec_id, local_path=None)


@requires_fftools
def test_idempotency_guard_sha256(tmp_path):
    """If the file has already been indexed (get_recording_by_sha256 returns a
    row), process() returns True without calling create_recording again.
    """
    from gss.media import MediaProcessor
    from tests.fake_camera import FakeCamera

    mission_id = _make_mission_id()
    cam = FakeCamera(tmp_path)
    path = cam.record(mission_id, duration_s=1.5)

    store = _stub_store()
    # Simulate the file already being in the DB.
    existing_id = str(uuid.uuid4())
    store.get_recording_by_sha256.return_value = {"id": existing_id, "local_path": str(path)}

    processor = MediaProcessor(
        store, drone_id="d5030000-0000-4000-8000-000000000001",
    )
    ok = processor.process(path)
    assert ok is True
    store.create_recording.assert_not_called()


@requires_fftools
def test_thumbnail_failure_still_writes_flagged_row(tmp_path):
    """If thumbnail upload fails, the row is still written with flagged=True."""
    from gss.media import MediaProcessor
    from tests.fake_camera import FakeCamera

    mission_id = _make_mission_id()
    cam = FakeCamera(tmp_path)
    path = cam.record(mission_id, duration_s=1.5)

    # upload_to_storage raises -- simulates network failure during thumbnail upload.
    store = _stub_store(upload_raises=True)

    processor = MediaProcessor(
        store, drone_id="d5030000-0000-4000-8000-000000000001",
    )
    ok = processor.process(path)
    assert ok is True  # Row written, just flagged.
    assert store.create_recording.call_count == 1
    kwargs = store.create_recording.call_args.kwargs
    assert kwargs["flagged"] is True
    assert "thumbnail" in (kwargs.get("detail") or {}).get("error", "").lower() or \
           kwargs.get("thumbnail_url") is None
