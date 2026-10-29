"""Audit log: gallery changes and failed password re-confirmations, newest first (ADR-0004, Phase 5).

Entries hold usernames, actions, ids and field names only: no names, notes, consent text or images.
"""

import base64
import binascii
import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..db import AuditEntry, as_utc
from ..deps import current_user, get_db

router = APIRouter(prefix="/api/audit-log", tags=["audit"], dependencies=[Depends(current_user)])

_ID = re.compile(r"^[a-f0-9]{32}$")


def _encode_cursor(entry: AuditEntry) -> str:
    raw = json.dumps([as_utc(entry.at).isoformat(), entry.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at, entry_id = json.loads(raw)
        if not (isinstance(at, str) and isinstance(entry_id, str) and _ID.match(entry_id)):
            raise ValueError
        value = datetime.fromisoformat(at)
        return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)), entry_id
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        raise HTTPException(422, "Invalid cursor") from None


@router.get("")
def list_audit_log(limit: int = Query(default=50, ge=1, le=200), cursor: str | None = Query(default=None, max_length=256),
                   db: Session = Depends(get_db)) -> dict:
    query = select(AuditEntry)
    if cursor:
        at, entry_id = _decode_cursor(cursor)
        query = query.where(or_(AuditEntry.at < at, and_(AuditEntry.at == at, AuditEntry.id < entry_id)))
    rows = list(db.scalars(query.order_by(AuditEntry.at.desc(), AuditEntry.id.desc()).limit(limit + 1)))
    page, more = rows[:limit], len(rows) > limit
    return {
        "items": [{"id": e.id, "at": as_utc(e.at).isoformat(), "username": e.username, "action": e.action,
                   "target_type": e.target_type, "target_id": e.target_id, "detail": e.detail} for e in page],
        "next_cursor": _encode_cursor(page[-1]) if more else None,
    }
