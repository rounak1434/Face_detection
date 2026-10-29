"""Apply approved saved devices to Frigate (ADR-0003, step 4).

Frigate 0.18.2 facts this relies on (checked in its source):
- `{FRIGATE_*}` values, including files in /run/secrets, are read once when
  Frigate starts (`frigate/config/env.py`).
- `/api/config/save` refuses a go2rtc stream source that is new compared with
  the saved file unless its placeholders resolve in the running process
  (`find_restricted_go2rtc_stream_changes`, `frigate/util/services.py`). So a
  newly added device needs a restart *before* the save, to load its secret.
- `/api/config/save` validates before writing (400 keeps the old file) and,
  with `save_option=restart`, restarts Frigate after writing.
- At every start, go2rtc's config is built from the saved file and the
  /run/secrets files (`docker/main/rootfs/usr/local/go2rtc/create_config.py`);
  a placeholder without a value stops go2rtc. So whenever Frigate may restart,
  the secret files cover both the file on disk and the one being saved: the
  apply writes old plus new secrets and removes the unused ones only at the end.

Sequence: build everything first (nothing changes on failure), keep Frigate's
current config and the current secret files, write the new secret files,
restart Frigate only when a device's secret is new, save the config with a
restart, then check that Frigate runs exactly the saved file with the expected
cameras. On any failure after the first change the previous secret files and
config are put back. URLs and passwords never appear in messages or logs.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import devices as dev
from .config import Settings
from .credentials import REPAIR_HINT, CredentialCipher, CredentialError
from .db import Device
from .frigate_client import FrigateClient, FrigateConfigRejected, FrigateError, FrigateUnreachable
from .frigate_config import FrigateDevice, render_config, secret_name
from .frigate_secrets import FrigateSecrets, SecretWriteError
from .frigate_secrets import validate as validate_secrets

SECRET_URL_RULE = "a single-line rtsp:// address of at most 2048 characters"

log = logging.getLogger(__name__)


class ApplyError(Exception):
    """The apply failed; `status` is the HTTP status for the route, the message is safe to show."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ApplyPlan:
    config_text: str
    secrets: dict[str, str]  # secret name -> URL (credential-bearing)
    cameras: list[dict]  # [{"device_id", "name", "camera"}]
    skipped: list[dict]  # approved but not connected: [{"device_id", "name", "reason"}]
    expected_cameras: set[str] = field(default_factory=set)
    _sensitive: list[str] = field(default_factory=list)

    def redact(self, text: str) -> str:
        for value in self._sensitive:
            if value:
                text = text.replace(value, "***")
        return text


def _detect_size(device: Device) -> tuple[int | None, int | None]:
    try:
        streams = json.loads(device.test_result) if device.test_result else {}
    except ValueError:
        return None, None
    stream = streams.get("sub" if device.sub_path else "main") or {}
    w, h = stream.get("width"), stream.get("height")
    return (w, h) if isinstance(w, int) and isinstance(h, int) else (None, None)


def build_plan(db: Session, cipher: CredentialCipher, template_text: str) -> ApplyPlan:
    """Everything the apply writes, computed without touching Frigate or the disk."""
    approved = list(db.scalars(select(Device).where(Device.use_in_frigate.is_(True)).order_by(Device.created_at)))
    ready = [d for d in approved if d.status == dev.CONNECTED]
    skipped = [{"device_id": d.id, "name": d.name, "reason": "connection test has not passed"}
               for d in approved if d.status != dev.CONNECTED]

    secrets: dict[str, str] = {}
    sensitive: list[str] = []
    for d in ready:
        password = None
        if d.password_enc:
            try:
                password = cipher.decrypt(d.password_enc, d.id)
            except CredentialError as exc:
                log.warning("Apply to Frigate stopped: device %s: %s", d.id, exc)
                raise ApplyError(409, f"{d.name}: {exc}. {REPAIR_HINT}. Nothing was changed in Frigate.") from None
        urls = {secret_name(d.id, stream): dev.build_url(d.host, d.rtsp_port, path, d.username, password)
                for stream, path in (("main", d.main_path), ("sub", d.sub_path)) if path}
        try:
            validate_secrets(urls)  # what the secret writer would refuse, refused here before anything changes
        except ValueError:
            raise ApplyError(422, f"{d.name}: its stream address cannot be used ({SECRET_URL_RULE}). "
                             "Edit the device and save it again. Nothing was changed in Frigate.") from None
        secrets.update(urls)
        sensitive.extend(urls.values())
        if password:
            sensitive.append(password)

    devices = [FrigateDevice(d.id, d.name, bool(d.sub_path), *_detect_size(d)) for d in ready]
    try:
        config_text, names = render_config(template_text, devices)
    except ValueError as exc:
        raise ApplyError(422, f"Frigate configuration could not be built: {exc}. Nothing was changed.") from None
    expected = set(yaml.safe_load(config_text)["cameras"])
    cameras = [{"device_id": d.id, "name": d.name, "camera": names[d.id]} for d in ready]
    return ApplyPlan(config_text, secrets, cameras, skipped, expected,
                     sorted(sensitive, key=len, reverse=True))


def older_copy(path):
    return path.with_name(path.name + ".1")


def _keep_previous(path, text: str) -> None:
    """Write Frigate's current config to `path`. A different earlier copy moves to `<path>.1`, so a retry
    after a failed restore cannot overwrite the only copy of the config before it. At most two copies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") != text:
        os.replace(path, older_copy(path))
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


async def wait_for_restart(frigate: FrigateClient, settings: Settings) -> None:
    """Wait until Frigate has stopped and then answers again."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    seen_down = False
    while True:
        up = await frigate.is_up()
        if not up:
            seen_down = True
        elif seen_down:
            return
        elapsed = loop.time() - start
        if not seen_down and elapsed >= settings.frigate_restart_down_seconds:
            raise FrigateError(f"Frigate did not restart within {settings.frigate_restart_down_seconds:g} s")
        if elapsed >= settings.frigate_restart_timeout_seconds:
            raise FrigateError(f"Frigate did not come back within {settings.frigate_restart_timeout_seconds:g} s")
        await asyncio.sleep(settings.frigate_restart_poll_seconds)


async def _verify(frigate: FrigateClient, config_text: str, expected: set[str]) -> None:
    if await frigate.config_raw() != config_text:
        raise FrigateError("Frigate is not running the saved configuration")
    missing = expected - set(await frigate.cameras())
    if missing:
        raise FrigateError(f"Frigate did not load the cameras {sorted(missing)}")


async def _restart(frigate: FrigateClient, settings: Settings) -> None:
    try:
        await frigate.restart()
    except FrigateUnreachable:
        pass  # the answer is often lost while Frigate stops; wait_for_restart decides
    await wait_for_restart(frigate, settings)


async def _save_and_restart(frigate: FrigateClient, settings: Settings, text: str) -> None:
    try:
        await frigate.save_config(text, restart=True)
    except FrigateUnreachable:
        pass  # the answer is often lost while Frigate restarts; the check afterwards decides
    await wait_for_restart(frigate, settings)


async def _restore(frigate: FrigateClient, settings: Settings, secrets: FrigateSecrets,
                   previous_secrets: dict[str, str], new_secrets: dict[str, str], previous_config: str) -> int:
    """Put the previous secret files and config back; returns the number of restarts."""
    if await frigate.config_raw() == previous_config:
        secrets.replace_all(previous_secrets)  # the config was never replaced
        return 0
    # Frigate's file is the new one: keep its secrets too (go2rtc needs them at the next start)
    # and restart to load the previous ones, which config/save needs to accept the old config.
    secrets.replace_all({**new_secrets, **previous_secrets})
    await _restart(frigate, settings)
    await _save_and_restart(frigate, settings, previous_config)
    await _verify(frigate, previous_config, set())
    secrets.replace_all(previous_secrets)
    return 2


async def apply(plan: ApplyPlan, frigate: FrigateClient, settings: Settings, secrets: FrigateSecrets) -> dict:
    try:
        previous_config = await frigate.config_raw()
    except FrigateError as exc:
        raise ApplyError(502, f"{exc}. Nothing was changed.") from None
    try:
        _keep_previous(settings.frigate_previous_config_file, previous_config)
    except OSError as exc:
        raise ApplyError(500, f"Could not keep the previous configuration ({exc.strerror}). Nothing was changed.") from None
    try:
        previous_secrets = secrets.read()
    except SecretWriteError as exc:
        raise ApplyError(500, f"{exc}. Nothing was changed.") from None

    new_names = set(plan.secrets) - set(previous_secrets)
    try:
        # Old and new: the file on disk still refers to the previous secrets until the save.
        secrets.replace_all({**previous_secrets, **plan.secrets})
    except ValueError:
        # Checked before any write: an existing secret file holds something replace_all would not write.
        raise ApplyError(500, "An existing Frigate secret file holds an unusable value; nothing was changed. "
                         "See TROUBLESHOOTING.md, \"Apply to Frigate fails\".") from None
    except SecretWriteError as exc:
        try:
            secrets.replace_all(previous_secrets)
        except SecretWriteError:
            raise ApplyError(500, f"{exc}; the previous secret files could not be put back either") from None
        raise ApplyError(500, f"{exc}. Nothing was changed.") from None

    restarts = 0
    try:
        if new_names:
            restarts += 1
            await _restart(frigate, settings)
        try:
            await _save_and_restart(frigate, settings, plan.config_text)
        except FrigateConfigRejected as exc:
            secrets.replace_all(previous_secrets)  # Frigate kept its config file
            raise ApplyError(422, "Frigate refused the configuration; its previous configuration is unchanged: "
                             + plan.redact(str(exc))) from None
        restarts += 1
        await _verify(frigate, plan.config_text, plan.expected_cameras)
    except ApplyError:
        raise
    except (FrigateError, SecretWriteError) as failure:
        reason = plan.redact(str(failure))
        log.warning("Apply to Frigate failed (%s); restoring the previous configuration", reason)
        try:
            restarts += await _restore(frigate, settings, secrets, previous_secrets, plan.secrets, previous_config)
        except (FrigateError, SecretWriteError) as exc:
            log.error("Restoring Frigate's previous configuration failed: %s", plan.redact(str(exc)))
            raise ApplyError(502, f"{reason}. Restoring the previous configuration also failed "
                             f"({plan.redact(str(exc))}). The outcome is UNKNOWN: Frigate may run the new or the "
                             "previous configuration once it is back; the secret files for both are kept so either "
                             f"can start. The previous configuration is kept in {settings.frigate_previous_config_file.name} "
                             "in the api data directory. Check Frigate before applying again.") from None
        raise ApplyError(502, f"{reason}. The previous configuration was restored.") from None

    try:
        secrets.replace_all(plan.secrets)  # drop the secrets the saved config no longer uses
    except SecretWriteError as exc:
        log.warning("Unused Frigate secret files could not be removed (%s); the next apply retries", exc)
    log.info("Applied %d saved device(s) to Frigate with %d restart(s)", len(plan.cameras), restarts)
    return {"cameras": plan.cameras, "skipped": plan.skipped, "restarts": restarts}
