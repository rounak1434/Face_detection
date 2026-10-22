from fastapi import APIRouter, Depends, HTTPException, Response, status

from ..deps import AppState, current_user, get_state, known_camera
from ..frigate_client import FrigateError

router = APIRouter(prefix="/api/cameras", tags=["cameras"], dependencies=[Depends(current_user)])


@router.get("")
async def list_cameras(state: AppState = Depends(get_state)) -> list[dict]:
    try:
        cameras = await state.cameras()
    except FrigateError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    # Only non-sensitive fields; stream URLs (which hold credentials) never leave the server.
    result = []
    for name, cfg in sorted(cameras.items()):
        detect = cfg.get("detect") or {}
        result.append(
            {
                "name": name,
                "enabled": cfg.get("enabled", True),
                "detect_width": detect.get("width"),
                "detect_height": detect.get("height"),
                "recording": bool((cfg.get("record") or {}).get("enabled")),
            }
        )
    return result


@router.get("/{camera}/latest.jpg")
async def latest_frame(camera: str = Depends(known_camera), state: AppState = Depends(get_state)) -> Response:
    """Current frame for the fallback live view. Not stored."""
    try:
        jpeg = await state.frigate.latest_jpeg(camera, height=480)
    except FrigateError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
