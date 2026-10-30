import asyncio

from fastapi import APIRouter, Depends

from .. import __version__
from ..deps import AppState, get_state
from ..frigate_client import FrigateError
from ..inference_client import InferenceUnavailable

router = APIRouter(tags=["health"])


async def _frigate(state: AppState) -> dict:
    try:
        return {"reachable": True, "version": await state.frigate.version()}
    except FrigateError as exc:
        return {"reachable": False, "error": str(exc)}


async def _inference(state: AppState) -> dict:
    try:
        health = await state.inference.health()
    except InferenceUnavailable as exc:
        return {"reachable": False, "error": str(exc)}
    # "ready" is true only when the detector is actually loaded and can run.
    return {"reachable": True, "models_ready": health.get("ready") is True}


@router.get("/api/health")
async def health(state: AppState = Depends(get_state)) -> dict:
    """Liveness of this service plus reachability of Frigate and the inference service. No secrets."""
    frigate, inference = await asyncio.gather(_frigate(state), _inference(state))
    return {
        "status": "ok",
        "app": state.settings.app_display_name,
        "version": __version__,
        "frigate": frigate,
        "inference": inference,
    }
