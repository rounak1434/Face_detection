"""Apply to Frigate: feed the approved saved devices to Frigate (ADR-0003).

Restarts Frigate once or twice (live view and recording pause), so it runs
only on an explicit request, one at a time. Responses never contain a URL
or password.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..deps import AppState, current_user, get_db, get_state
from ..frigate_apply import ApplyError, apply, build_plan
from ..frigate_secrets import FrigateSecrets

router = APIRouter(prefix="/api/frigate", tags=["frigate"], dependencies=[Depends(current_user)])


@router.post("/apply")
async def apply_to_frigate(db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    settings = state.settings
    if settings.frigate_config_template is None or settings.frigate_secrets_dir is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Apply to Frigate is not set up: FRIGATE_CONFIG_TEMPLATE and FRIGATE_SECRETS_DIR are needed")
    if state.frigate_apply_lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "An apply to Frigate is already running")
    async with state.frigate_apply_lock:
        try:
            template = settings.frigate_config_template.read_text(encoding="utf-8")
            secrets = FrigateSecrets(settings.frigate_secrets_dir)
        except OSError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                f"Apply to Frigate cannot read its files ({exc.strerror}). Nothing was changed.") from None
        try:
            plan = build_plan(db, state.cipher, template)
            result = await apply(plan, state.frigate, settings, secrets)
        except ApplyError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        finally:
            state._cameras = None  # the camera list may have changed
    return result
