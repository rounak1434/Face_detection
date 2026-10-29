"""Camera & NVR setup: CRUD and connection tests for configured devices.

Rules enforced here:
- passwords are write-only: stored encrypted, never returned or logged;
- tests run only for saved devices, by id; there is no "probe this URL" endpoint;
- device hosts are checked against the allowed networks at save and test time.
"""

import ipaddress
import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import devices as dev
from ..credentials import CredentialError
from ..db import Device, as_utc, utcnow
from ..deps import AppState, current_user, get_db, get_state
from ..frigate_config import MAX_DEVICES as MAX_FRIGATE_DEVICES
from ..netguard import AddressNotAllowed, check_ip, parse_networks, validate_host_syntax

router = APIRouter(prefix="/api/devices", tags=["devices"], dependencies=[Depends(current_user)])
log = logging.getLogger(__name__)

_CONTROL = set(chr(c) for c in range(32)) | {chr(127)}


def _no_control_chars(value: str | None, field: str) -> str | None:
    if value is not None and any(ch in _CONTROL for ch in value):
        raise ValueError(f"{field} contains control characters")
    return value


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern="^(ip_camera|nvr)$")
    host: str = Field(min_length=1, max_length=253)
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=64)
    # Write-only. On update: None keeps the stored password; "" with
    # clear_password removes it.
    password: str | None = Field(default=None, max_length=128)
    clear_password: bool = False
    main_stream: str = Field(min_length=1, max_length=600)
    sub_stream: str | None = Field(default=None, max_length=600)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return _no_control_chars(v, "Name")

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return validate_host_syntax(v)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v and ":" in v:
            raise ValueError("Username must not contain ':'")
        return _no_control_chars(v, "Username")

    @field_validator("password")
    @classmethod
    def _password(cls, v: str | None) -> str | None:
        return _no_control_chars(v, "Password")

    @model_validator(mode="after")
    def _streams(self):
        self.main_stream = dev.normalize_stream_path(self.main_stream, self.host, self.rtsp_port)
        sub = (self.sub_stream or "").strip()
        self.sub_stream = dev.normalize_stream_path(sub, self.host, self.rtsp_port) if sub else None
        return self


def _check_literal_ip(host: str, state: AppState) -> None:
    """Refuse IP literals outside the allowed networks already at save time.

    Host names are checked when tested, because their addresses can change.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    try:
        check_ip(ip, parse_networks(state.settings.device_allowed_networks))
    except AddressNotAllowed as exc:
        raise HTTPException(422, str(exc)) from exc


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def to_dict(d: Device) -> dict:
    """Browser view of a device. Contains no password and no credential-bearing URL."""
    has_password = bool(d.password_enc)
    has_credentials = bool(d.username) or has_password
    streams = json.loads(d.test_result) if d.test_result else {}
    return {
        "id": d.id,
        "name": d.name,
        "kind": d.kind,
        "source_label": dev.SOURCE_LABELS[d.kind],
        "host": d.host,
        "rtsp_port": d.rtsp_port,
        "username": d.username,
        "has_password": has_password,
        "main_stream": d.main_path,
        "sub_stream": d.sub_path,
        "main_url_display": dev.display_url(d.host, d.rtsp_port, d.main_path, has_credentials),
        "sub_url_display": dev.display_url(d.host, d.rtsp_port, d.sub_path, has_credentials) if d.sub_path else None,
        "status": d.status,
        "status_detail": d.status_detail,
        "tested_at": _iso(d.tested_at),
        "streams": streams,
        "editable": d.kind != "synthetic",
        "use_in_frigate": d.use_in_frigate,
    }


def _get(db: Session, device_id: str) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    return device


def _ensure_unique_name(db: Session, name: str, exclude_id: str | None = None) -> None:
    query = select(Device.id).where(Device.name == name)
    if exclude_id:
        query = query.where(Device.id != exclude_id)
    if db.scalar(query):
        raise HTTPException(status.HTTP_409_CONFLICT, "A device with this name already exists")


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A device with this name already exists") from exc


@router.get("")
def list_devices(db: Session = Depends(get_db)) -> list[dict]:
    return [to_dict(d) for d in db.scalars(select(Device).order_by(Device.created_at))]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_device(body: DeviceIn, db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    _check_literal_ip(body.host, state)
    _ensure_unique_name(db, body.name)
    device = Device(
        name=body.name, kind=body.kind, host=body.host, rtsp_port=body.rtsp_port,
        username=body.username, main_path=body.main_stream, sub_path=body.sub_stream,
    )
    db.add(device)
    db.flush()  # id is needed as encryption context
    if body.password:
        device.password_enc = state.cipher.encrypt(body.password, device.id)
    _commit(db)
    log.info("Device %s (%s) created", device.id, device.kind)
    return to_dict(device)


@router.post("/synthetic", status_code=status.HTTP_201_CREATED)
def add_synthetic(db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    """Add (once) the server-defined synthetic RTSP source. Its address is not client input."""
    existing = db.scalar(select(Device).where(Device.kind == "synthetic"))
    if existing:
        return to_dict(existing)
    host, port, path = dev.synthetic_target(state.settings)
    device = Device(name="Synthetic test stream", kind="synthetic", host=host, rtsp_port=port, main_path=path)
    db.add(device)
    _commit(db)
    return to_dict(device)


@router.put("/{device_id}")
def update_device(device_id: str, body: DeviceIn, db: Session = Depends(get_db),
                  state: AppState = Depends(get_state)) -> dict:
    device = _get(db, device_id)
    if device.kind == "synthetic":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The synthetic source is defined by the server")
    _check_literal_ip(body.host, state)
    _ensure_unique_name(db, body.name, exclude_id=device.id)
    changed = (device.kind, device.host, device.rtsp_port, device.username, device.main_path, device.sub_path) != (
        body.kind, body.host, body.rtsp_port, body.username, body.main_stream, body.sub_stream)
    device.name, device.kind, device.host, device.rtsp_port = body.name, body.kind, body.host, body.rtsp_port
    device.username, device.main_path, device.sub_path = body.username, body.main_stream, body.sub_stream
    if body.clear_password:
        device.password_enc, changed = None, True
    elif body.password:
        device.password_enc, changed = state.cipher.encrypt(body.password, device.id), True
    if changed:
        # A previous result says nothing about the new settings.
        device.status, device.status_detail, device.test_result, device.tested_at = dev.NOT_TESTED, None, None, None
    device.updated_at = utcnow()
    _commit(db)
    log.info("Device %s updated", device.id)
    return to_dict(device)


class FrigateUseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_in_frigate: StrictBool


@router.put("/{device_id}/frigate")
def set_use_in_frigate(device_id: str, body: FrigateUseIn, db: Session = Depends(get_db)) -> dict:
    """Approve or withdraw a device for "Apply to Frigate" (ADR-0003). Nothing reaches Frigate here.

    Switching on needs a successful connection test. A device whose settings
    change later is reset to "not tested" and skipped by the apply until it
    passes a test again; the switch itself stays as the operator set it.
    """
    device = _get(db, device_id)
    if body.use_in_frigate and not device.use_in_frigate:
        if device.status != dev.CONNECTED:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Test the connection successfully before using this device in Frigate")
        approved = db.scalar(select(func.count()).select_from(Device).where(Device.use_in_frigate.is_(True)))
        if approved >= MAX_FRIGATE_DEVICES:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"At most {MAX_FRIGATE_DEVICES} devices can be used in Frigate")
    if device.use_in_frigate != body.use_in_frigate:
        device.use_in_frigate = body.use_in_frigate
        device.updated_at = utcnow()
        db.commit()
        log.info("Device %s %s for Frigate", device.id, "approved" if body.use_in_frigate else "withdrawn")
    return to_dict(device)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: str, db: Session = Depends(get_db)) -> None:
    db.delete(_get(db, device_id))
    db.commit()
    log.info("Device %s removed", device_id)


@router.post("/{device_id}/test")
async def test_connection(device_id: str, db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    device = _get(db, device_id)
    now = time.monotonic()
    last = state.device_tests.get(device_id)
    if last is not None and now - last < state.settings.device_test_cooldown_seconds:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait a few seconds before testing again")
    state.device_tests[device_id] = now

    password = None
    if device.password_enc:
        try:
            password = state.cipher.decrypt(device.password_enc, device.id)
        except CredentialError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{exc}. Re-enter the password.") from exc

    results = await dev.test_device(
        state.settings, device.kind, device.host, device.rtsp_port,
        device.main_path, device.sub_path, device.username, password,
    )
    main = results["main"]
    device.status, device.status_detail = main.status, main.detail
    device.test_result = dev.results_to_json(results)
    device.tested_at = utcnow()
    db.commit()
    log.info("Device %s tested: %s", device.id, ", ".join(f"{k}={v.status}" for k, v in results.items()))
    return to_dict(device)
