"""Live view: relays go2rtc's MSE websocket (served by Frigate) to the browser.

The browser connects only to this service. The session cookie is checked
and the Origin must match the Host, so other sites cannot open the stream.
"""

import asyncio
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from ..frigate_client import FrigateError, live_stream_name
from ..security import SESSION_COOKIE, user_for_token

router = APIRouter(tags=["live"])
log = logging.getLogger(__name__)


def origin_allowed(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


@router.websocket("/api/cameras/{camera}/live")
async def live(websocket: WebSocket, camera: str) -> None:
    state = websocket.app.state.app

    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    with state.db.sessionmaker() as db:
        user = user_for_token(db, websocket.cookies.get(SESSION_COOKIE))
    if user is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        cameras = await state.cameras()
        if camera not in cameras:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        stream = live_stream_name(camera, cameras[camera])
        headers = await state.frigate.auth_headers()
    except FrigateError:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await websocket.accept()
    try:
        async with connect(state.frigate.live_ws_url(stream), additional_headers=headers, max_size=None) as upstream:

            async def browser_to_upstream() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_browser() -> None:
                async for msg in upstream:
                    if isinstance(msg, bytes):
                        await websocket.send_bytes(msg)
                    else:
                        await websocket.send_text(msg)

            tasks = [asyncio.create_task(browser_to_upstream()), asyncio.create_task(upstream_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except (OSError, InvalidHandshake) as exc:
        log.warning("Live stream for %s unavailable: %s", camera, type(exc).__name__)
    except (ConnectionClosed, WebSocketDisconnect):
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed
