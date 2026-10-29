"""Face events: Frigate person events analysed once for faces and stored (ADR-0004, Phase 3).

The ingestion loop polls Frigate for finished person events, runs face detection
on each new one (face_detection.py) and stores the result:

- One row per Frigate event (unique `frigate_event_id`): an event is never stored
  twice, across polls, overlapping pages or restarts.
- Restart-safe cursor: each poll asks for events that started after the newest
  stored event minus FACE_EVENT_LOOKBACK_SECONDS (Frigate filters by start time,
  and a long event appears only once it has ended); already stored ones are skipped.
- Events are processed oldest first. A temporary failure (Frigate or the inference
  service unreachable, model not loaded, disk full) stops the poll, so no later event
  overtakes it; after FACE_MAX_ATTEMPTS tries the event is recorded as `gave_up`.
- Images: the frame and the face crops are stored only for events with faces, in
  media/faces with server-generated names. Files are written before the rows; if the
  rows cannot be written, the files are removed again.
- Retention: FACE_EVENT_RETENTION_DAYS, files first, then the row, one commit each.
- Recognition (Phase 6, recognition.py): only while the switch is on. Each poll first
  embeds reference faces that have no embedding yet and loads the gallery; each event is
  then analysed with /v1/embed and every face is matched. With the switch off only
  /v1/detect is called and faces are stored as NOT_MATCHED. Event embeddings are not stored.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from . import recognition
from .captures import InsufficientStorage, check_free_space
from .config import Settings
from .credentials import CredentialCipher
from .db import FaceDetection, FaceEvent, Person, as_utc, new_id, utcnow
from .face_detection import FACES_DETECTED, GAVE_UP, EventFaces, detect_event_faces
from .gallery import FACE_EVENT_DELETED, audit
from .frigate_client import FrigateClient, FrigateError, PersonEvent
from .inference_client import InferenceClient, InferenceUnavailable
from .media import MediaStore

log = logging.getLogger(__name__)

PAGE_SIZE = 50
MAX_PAGES_PER_POLL = 20
RETENTION_CHECK_SECONDS = 3600


@dataclass
class IngestStats:
    stored: int = 0
    known: int = 0
    stopped: str | None = None  # why the poll ended early, if it did


@dataclass
class Ingestor:
    settings: Settings
    frigate: FrigateClient
    inference: InferenceClient
    sessions: sessionmaker
    store: MediaStore
    cipher: CredentialCipher | None = None  # with gallery_store: needed for recognition
    gallery_store: MediaStore | None = None
    attempts: dict[str, int] = field(default_factory=dict)  # Frigate event id -> temporary failures
    embed_failures: set[str] = field(default_factory=set)  # reference faces that could not be embedded
    _last_problem: str | None = None

    async def _prepare_recognition(self) -> str | None:
        """Once per poll, when recognition is on: the embedder id, after embedding pending references."""
        embedder, status = await recognition.current_embedder(self.inference)
        if embedder is None:
            log.info("Recognition is on but the embedder is not ready (%s); detection only", status)
            return None
        try:
            await recognition.embed_missing_references(self.sessions, self.inference, self.cipher, self.gallery_store,
                                                       self.settings, embedder, self.embed_failures)
        except InferenceUnavailable as exc:
            log.info("Recognition is on but reference faces cannot be embedded now (%s); detection only", exc)
            return None
        return embedder

    def _switch_and_gallery(self, embedder: str | None) -> tuple[bool, recognition.Gallery | None]:
        """Read before every event, so switching off, deactivating or deleting someone applies to the
        very next event, not the next poll (privacy review, Phase 8)."""
        with self.sessions() as db:
            enabled = recognition.is_enabled(db)
        if not enabled or embedder is None:
            return enabled, None
        return True, recognition.load_gallery(self.sessions, self.cipher, embedder)

    def _cursor(self) -> float:
        with self.sessions() as db:
            newest = db.scalar(select(func.max(FaceEvent.started_at)))
        if newest is None:
            return (utcnow() - timedelta(seconds=self.settings.face_backfill_seconds)).timestamp()
        return as_utc(newest).timestamp() - self.settings.face_event_lookback_seconds

    def _known(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with self.sessions() as db:
            return set(db.scalars(select(FaceEvent.frigate_event_id).where(FaceEvent.frigate_event_id.in_(ids))))

    def _problem(self, message: str | None) -> None:
        """Log a lasting problem once, and its end, instead of every poll."""
        if message != self._last_problem:
            if message:
                log.warning("Face events paused: %s", message)
            elif self._last_problem:
                log.info("Face events resumed")
            self._last_problem = message

    async def ingest_once(self) -> IngestStats:
        stats = IngestStats()
        after, offset = self._cursor(), 0
        embedder: str | None = None
        prepared = False  # _prepare_recognition ran in this poll
        for _ in range(MAX_PAGES_PER_POLL):
            try:
                events, returned = await self.frigate.person_events_page(after, PAGE_SIZE, offset)
            except FrigateError as exc:
                self._problem(f"Frigate events unavailable ({exc})")
                stats.stopped = "frigate_unavailable"
                return stats
            known = self._known([e.id for e in events])
            for event in events:
                if event.id in known:
                    stats.known += 1
                    continue
                try:
                    check_free_space(self.store, self.settings)
                except InsufficientStorage as exc:
                    self._problem(str(exc))
                    stats.stopped = "insufficient_storage"
                    return stats
                with self.sessions() as db:
                    switched_on = recognition.is_enabled(db)
                if switched_on and not prepared and self.cipher is not None and self.gallery_store is not None:
                    embedder = await self._prepare_recognition()  # once per poll, only when needed
                    prepared = True
                enabled, gallery = self._switch_and_gallery(embedder)
                result = await self._detect(event, gallery is not None)
                if result is None:
                    stats.stopped = "temporary_failure"
                    return stats
                self._match(result, enabled, gallery)
                try:
                    stored = self.save(result)
                except (OSError, SQLAlchemyError) as exc:
                    self._problem(f"face event {event.id} could not be stored ({type(exc).__name__})")
                    stats.stopped = "storage_error"
                    return stats
                self.attempts.pop(event.id, None)
                stats.stored += int(stored)
            if returned < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        self._problem(None)
        return stats

    def _match(self, result: EventFaces, enabled: bool, gallery: recognition.Gallery | None) -> None:
        """Set each face's match; fix the recognition state when recognition could not run."""
        if gallery is None:
            result.recognition_state = recognition.STATE_UNAVAILABLE if enabled else recognition.STATE_OFF
            return
        if result.recognition_state == recognition.STATE_DONE and result.faces and result.embedder != gallery.embedder:
            log.warning("Event %s embedded by %s, gallery by %s: not matched", result.event.id, result.embedder,
                        gallery.embedder)
            result.recognition_state = recognition.STATE_UNAVAILABLE
        if result.recognition_state != recognition.STATE_DONE:
            return
        result.embedder = gallery.embedder
        for face in result.faces:
            face.match = recognition.match_face(face.box, face.embedding, gallery, self.settings)

    async def _detect(self, event: PersonEvent, recognize: bool = False) -> EventFaces | None:
        """The result, or None to stop this poll and try the event again later."""
        try:
            return await detect_event_faces(event, self.frigate, self.inference, self.settings, recognize)
        except (FrigateError, InferenceUnavailable) as exc:
            tries = self.attempts.get(event.id, 0) + 1
            self.attempts[event.id] = tries
            if tries < self.settings.face_max_attempts:
                self._problem(f"event {event.id}: {exc} (try {tries} of {self.settings.face_max_attempts})")
                return None
            log.warning("Face event %s recorded as %s after %d tries: %s", event.id, GAVE_UP, tries, exc)
            return EventFaces(event, GAVE_UP, detail=f"not analysed after {tries} tries: {exc}"[:500],
                              recognition_state=recognition.STATE_UNAVAILABLE if recognize else recognition.STATE_OFF)

    def save(self, result: EventFaces) -> bool:
        """Store one analysed event. False if it was stored meanwhile. Raises OSError/SQLAlchemyError."""
        written: list[str] = []
        keep_images = result.status == FACES_DETECTED
        try:
            frame_file = None
            if keep_images and result.frame_jpeg:
                frame_file = f"{new_id()}.jpg"
                self.store.write_atomic(frame_file, result.frame_jpeg)
                written.append(frame_file)
            crops = []
            for face in result.faces if keep_images else []:
                crop_file = None
                if face.crop_jpeg:
                    crop_file = f"{new_id()}.jpg"
                    self.store.write_atomic(crop_file, face.crop_jpeg)
                    written.append(crop_file)
                crops.append(crop_file)
            with self.sessions() as db:
                event = FaceEvent(
                    frigate_event_id=result.event.id, camera=result.event.camera,
                    started_at=datetime.fromtimestamp(result.event.start_time, UTC),
                    ended_at=datetime.fromtimestamp(result.event.end_time, UTC),
                    status=result.status, face_count=len(result.faces) if keep_images else 0,
                    frame_source=result.frame_source, frame_time=result.frame_time, frame_file=frame_file,
                    frame_width=result.frame_width, frame_height=result.frame_height,
                    detector=f"{result.model.get('name')} {result.model.get('version')}" if result.model else None,
                    detail=result.detail[:500] if result.detail else None,
                    recognition_state=result.recognition_state,
                    recognizer=result.embedder if result.recognition_state == recognition.STATE_DONE else None,
                )
                db.add(event)
                db.flush()
                faces = result.faces if keep_images else []
                # A matched person deleted since the gallery was loaded is not linked.
                matched = {f.match.person_id for f in faces if f.match is not None and f.match.person_id}
                existing = set(db.scalars(select(Person.id).where(Person.id.in_(matched)))) if matched else set()
                for position, (face, crop_file) in enumerate(zip(faces, crops, strict=True)):
                    box, m = face.box, face.match
                    db.add(FaceDetection(
                        event_id=event.id, position=position, box_x=int(box["x"]), box_y=int(box["y"]),
                        box_w=int(box["w"]), box_h=int(box["h"]), score=face.score,
                        keypoints=json.dumps(face.keypoints5), quality=json.dumps(face.quality), crop_file=crop_file,
                        outcome=m.outcome if m is not None else recognition.NOT_MATCHED,
                        person_id=m.person_id if m is not None and m.person_id in existing else None,
                        similarity=m.similarity if m is not None else None,
                        accept_threshold=self.settings.recognition_accept_threshold if m is not None else None,
                        reject_threshold=self.settings.recognition_reject_threshold if m is not None else None,
                    ))
                db.commit()
            return True
        except IntegrityError:
            self._remove(written)
            return False  # the same Frigate event was stored meanwhile
        except BaseException:
            self._remove(written)
            raise

    def _remove(self, files: list[str]) -> None:
        for name in files:
            try:
                self.store.delete(name)
            except OSError as exc:
                log.warning("Could not remove face image %s: %s", name, exc)


def delete_face_event(sessions: sessionmaker, store: MediaStore, event_id: str, username: str | None = None) -> bool:
    """Delete a face event: its files first, then the rows (detections cascade).

    False if it does not exist. A file that cannot be removed keeps the event, so
    nothing is orphaned on disk (raises OSError). With `username` (an operator's
    deletion, not retention) the deletion is audited in the same commit.
    """
    with sessions() as db:
        event = db.get(FaceEvent, event_id)
        if event is None:
            return False
        files = [event.frame_file] + list(db.scalars(select(FaceDetection.crop_file).where(FaceDetection.event_id == event_id)))
        for name in filter(None, files):
            try:
                store.delete(name)
            except ValueError:
                pass  # not a name we generate: no file of ours
        db.execute(delete(FaceDetection).where(FaceDetection.event_id == event_id))
        db.execute(delete(FaceEvent).where(FaceEvent.id == event_id))
        if username:
            audit(db, username, FACE_EVENT_DELETED, "face_event", event_id)
        db.commit()
    return True


def purge_old_face_events(sessions: sessionmaker, store: MediaStore, days: int) -> int:
    """Delete face events that started more than `days` ago, one commit each."""
    cutoff = utcnow() - timedelta(days=days)
    with sessions() as db:
        ids = list(db.scalars(select(FaceEvent.id).where(FaceEvent.started_at < cutoff)))
    removed = 0
    for event_id in ids:
        try:
            removed += int(delete_face_event(sessions, store, event_id))
        except OSError as exc:
            log.warning("Retention: could not remove the files of face event %s: %s", event_id, exc)
    return removed


async def face_ingest_loop(ingestor: Ingestor) -> None:
    while True:
        try:
            stats = await ingestor.ingest_once()
            if stats.stored:
                log.info("Face events: stored %d new event(s)", stats.stored)
        except Exception:
            log.exception("Face event ingestion failed; retrying")
        await asyncio.sleep(ingestor.settings.face_poll_seconds)


async def face_retention_loop(sessions: sessionmaker, store: MediaStore, days: int) -> None:
    while True:
        try:
            removed = await asyncio.to_thread(purge_old_face_events, sessions, store, days)
            if removed:
                log.info("Retention: deleted %d face event(s) older than %d days", removed, days)
        except Exception:
            log.exception("Face event retention failed; retrying in an hour")
        await asyncio.sleep(RETENTION_CHECK_SECONDS)
