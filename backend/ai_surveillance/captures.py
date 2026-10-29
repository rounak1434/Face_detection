"""Snapshot and clip capture.

Snapshots are taken synchronously from Frigate's latest frame. Clips are
exported from Frigate's recordings in a background task, because Frigate
needs time to flush the last recording segment and queues exports.
"""

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from .config import Settings
from .db import Capture, utcnow
from .frigate_client import FrigateClient, FrigateError
from .media import MediaStore, describe_os_error

log = logging.getLogger(__name__)

RETENTION_CHECK_SECONDS = 3600.0
_MB = 1024 * 1024


class InsufficientStorage(Exception):
    """Too little free space to save another capture."""


def check_free_space(store: MediaStore, settings: Settings) -> None:
    """Refuse captures before they can fill the volume the database lives on."""
    floor = settings.capture_min_free_mb
    if floor <= 0:
        return
    free_mb = store.free_bytes() // _MB
    if free_mb < floor:
        raise InsufficientStorage(
            f"Not enough free disk space for captures: {free_mb} MB free, at least {floor} MB is kept free "
            "for the database (CAPTURE_MIN_FREE_MB). Delete old captures to free space."
        )


async def take_snapshot(db, frigate: FrigateClient, store: MediaStore, camera: str) -> Capture:
    capture = Capture(camera=camera, kind="snapshot", status="pending")
    db.add(capture)
    db.flush()  # assigns the id
    try:
        # Frigate returns a placeholder image (HTTP 200) for an offline
        # camera, so check that frames are actually arriving first.
        fps = await frigate.camera_fps(camera)
        if fps is not None and fps <= 0:
            raise FrigateError("Camera is offline: Frigate is receiving no frames")
        jpeg, offline = await frigate.latest_jpeg(camera)
        if offline:
            raise FrigateError("Camera is offline: Frigate returned a fallback frame")
        capture.file_name = f"{capture.id}.jpg"
        capture.size_bytes = store.write_atomic(capture.file_name, jpeg)
        capture.status = "ready"
    except FrigateError as exc:
        capture.status = "failed"
        capture.error = str(exc)[:500]
    except OSError as exc:
        log.warning("Snapshot %s could not be saved: %s", capture.id, exc)
        capture.status = "failed"
        capture.error = describe_os_error("snapshot", exc)
    db.commit()
    return capture


def create_clip_request(db, camera: str, seconds: int) -> Capture:
    capture = Capture(camera=camera, kind="clip", status="pending", duration_seconds=seconds)
    db.add(capture)
    db.commit()
    return capture


async def wait_for_export(frigate: FrigateClient, export_id: str, settings: Settings) -> str:
    """Poll a queued Frigate export until it finishes; return its video_path."""
    deadline = time.monotonic() + settings.clip_export_timeout_seconds
    while True:
        job = await frigate.export_job(export_id)
        if job is not None and job.get("status") == "failed":
            raise FrigateError(f"Frigate export failed: {job.get('error_message') or 'no reason given'}")
        if job is not None and job.get("status") == "cancelled":
            raise FrigateError("Frigate export was cancelled")
        if job is None or job.get("status") == "success":
            # Finished jobs are forgotten after a few seconds; the stored
            # export row is the lasting record.
            record = await frigate.export_record(export_id)
            if record and not record.get("in_progress") and record.get("video_path"):
                return record["video_path"]
        if time.monotonic() > deadline:
            raise FrigateError("Timed out waiting for Frigate to finish the export")
        await asyncio.sleep(settings.clip_poll_interval_seconds)


async def run_clip_export(
    capture_id: str,
    end_ts: int,
    settings: Settings,
    frigate: FrigateClient,
    store: MediaStore,
    sessions: sessionmaker,
) -> None:
    """Export [end_ts - duration, end_ts] from Frigate and store it locally."""
    with sessions() as db:
        capture = db.get(Capture, capture_id)
        if capture is None:
            return
        camera, seconds = capture.camera, capture.duration_seconds or settings.clip_default_seconds

    error: str | None = None
    file_name = f"{capture_id}.mp4"
    try:
        await asyncio.sleep(settings.clip_settle_seconds)
        export_id = await frigate.start_export(camera, end_ts - seconds, end_ts, name=f"capture-{capture_id}")
        with sessions() as db:
            row = db.get(Capture, capture_id)
            row.frigate_export_id = export_id
            db.commit()

        video_path = await wait_for_export(frigate, export_id, settings)
        check_free_space(store, settings)  # space may have run out while Frigate exported
        async with frigate.stream_export_file(video_path) as resp:
            with store.partial_path(file_name).open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
        size = store.finalize(file_name)
    except (FrigateError, InsufficientStorage) as exc:
        error = str(exc)[:500]
        log.warning("Clip export %s failed: %s", capture_id, error)
    except OSError as exc:
        # The full error (with the path) goes to the log only.
        error = describe_os_error("clip", exc)
        log.warning("Clip export %s failed: %s", capture_id, exc)
    except Exception:
        # Never leave a capture stuck in "pending".
        error = "Unexpected error while exporting the clip; see service logs"
        log.exception("Clip export %s failed unexpectedly", capture_id)
    if error is not None:
        try:
            store.delete(file_name)
        except OSError as exc:
            # Removed at the next startup; the capture must still be marked failed.
            log.warning("Could not remove the partial clip %s: %s", capture_id, exc)

    with sessions() as db:
        row = db.get(Capture, capture_id)
        if row is None:
            return
        if error is None:
            row.status, row.file_name, row.size_bytes = "ready", file_name, size
        else:
            row.status, row.error = "failed", error
        db.commit()


def purge_old_captures(sessions: sessionmaker, store: MediaStore, days: int) -> int:
    """Delete finished captures older than `days`: the file first, then the record.

    Pending clips are never touched. A file that cannot be removed keeps its
    record, so nothing is orphaned on disk; it is retried at the next check.
    """
    cutoff = utcnow() - timedelta(days=days)
    removed = 0
    with sessions() as db:
        rows = db.query(Capture).filter(Capture.status != "pending", Capture.created_at < cutoff).all()
        for row in rows:
            if row.file_name:
                try:
                    store.delete(row.file_name)
                except ValueError:
                    pass  # not a name we generate: no file of ours to remove
                except OSError as exc:
                    log.warning("Retention: could not remove the file of capture %s: %s", row.id, exc)
                    continue
            db.delete(row)
            removed += 1
        db.commit()
    return removed


async def retention_loop(sessions: sessionmaker, store: MediaStore, days: int) -> None:
    """Apply the retention period at startup, then every hour, until cancelled."""
    while True:
        try:
            removed = await asyncio.to_thread(purge_old_captures, sessions, store, days)
            if removed:
                log.info("Retention: deleted %d capture(s) older than %d days", removed, days)
        except Exception:
            log.exception("Retention check failed; retrying in an hour")
        await asyncio.sleep(RETENTION_CHECK_SECONDS)


def fail_stale_pending(sessions: sessionmaker) -> int:
    """Clip tasks do not survive a restart; mark leftovers as failed."""
    with sessions() as db:
        rows = db.query(Capture).filter(Capture.status == "pending").all()
        for row in rows:
            row.status = "failed"
            row.error = "Interrupted by a service restart"
        db.commit()
        return len(rows)
