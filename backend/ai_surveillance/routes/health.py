from fastapi import APIRouter, Depends

from .. import __version__
from ..deps import AppState, get_state
from ..frigate_client import FrigateError

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(state: AppState = Depends(get_state)) -> dict:
    """Liveness of this service plus reachability of Frigate. No secrets."""
    try:
        frigate = {"reachable": True, "version": await state.frigate.version()}
    except FrigateError as exc:
        frigate = {"reachable": False, "error": str(exc)}
    return {
        "status": "ok",
        "app": state.settings.app_display_name,
        "version": __version__,
        "frigate": frigate,
    }
