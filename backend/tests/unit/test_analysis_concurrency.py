"""Backpressure and cleanup of /api/analysis/faces under concurrent load.

Runs the real app in-process (httpx ASGI transport) with the inference
service mocked by respx. Checks that at most `analysis_concurrency` uploads
are processed at once, that waiting requests time out with 503, that slow
uploads get 408, and that the slot is always released (errors, timeouts,
client cancellation).
"""

import asyncio

import httpx
import pytest

from ai_surveillance.db import AdminUser, Database
from ai_surveillance.main import create_app
from ai_surveillance.security import hash_password

from .conftest import ADMIN_PASSWORD, ADMIN_USER, INFERENCE

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 200
NO_FACES = {"status": "no_faces_detected", "image": {"width": 10, "height": 10, "format": "image/jpeg"},
            "faces": [], "face_count": 0, "parameters": None, "model": {"name": "yunet", "version": "2023mar"},
            "processing_ms": 1.0}


def _files(data=JPEG):
    return {"file": ("x.jpg", data, "image/jpeg")}


@pytest.fixture
def make_app(settings, frigate):
    db = Database(settings.database_url)
    db.migrate()
    with db.sessionmaker() as s:
        s.add(AdminUser(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASSWORD)))
        s.commit()
    db.engine.dispose()
    return lambda: create_app(settings)


async def _run(app, scenario):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
            assert resp.status_code == 200
            return await scenario(client, app.state.app)


def _slot_value(state) -> int:
    return state.analysis_slots._value  # asyncio.Semaphore internal counter


def test_concurrency_is_limited(make_app, frigate, settings):
    settings.analysis_concurrency = 2
    active, peak = 0, 0

    async def slow_detect(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.15)
        active -= 1
        return httpx.Response(200, json=NO_FACES)

    frigate.post(f"{INFERENCE}/v1/detect").mock(side_effect=slow_detect)

    async def scenario(client, state):
        results = await asyncio.gather(*(client.post("/api/analysis/faces", files=_files()) for _ in range(6)))
        assert [r.status_code for r in results] == [200] * 6
        assert _slot_value(state) == 2

    asyncio.run(_run(make_app(), scenario))
    assert peak == 2


def test_waiting_too_long_gives_503_busy(make_app, frigate, settings):
    settings.analysis_concurrency = 1
    settings.analysis_queue_timeout_seconds = 0.1

    release = asyncio.Event()

    async def slow_detect(request):
        await release.wait()  # holds the only slot until the second request has been refused
        return httpx.Response(200, json=NO_FACES)

    frigate.post(f"{INFERENCE}/v1/detect").mock(side_effect=slow_detect)

    async def scenario(client, state):
        first = asyncio.create_task(client.post("/api/analysis/faces", files=_files()))
        # Wait until the first request really holds the slot. A fixed sleep here
        # was flaky on slow machines: the second request could take it first.
        for _ in range(500):
            if _slot_value(state) == 0:
                break
            await asyncio.sleep(0.01)
        assert _slot_value(state) == 0
        second = await client.post("/api/analysis/faces", files=_files())
        release.set()
        assert second.status_code == 503
        assert second.json()["error"] == "analysis_busy" and second.headers["retry-after"] == "5"
        assert (await first).status_code == 200
        assert _slot_value(state) == 1

    asyncio.run(_run(make_app(), scenario))


def test_slow_upload_gives_408_and_frees_slot(make_app, frigate, settings):
    settings.analysis_concurrency = 1
    settings.analysis_upload_timeout_seconds = 0.2
    route = frigate.post(f"{INFERENCE}/v1/detect").respond(200, json=NO_FACES)

    async def trickle():
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.jpg\"\r\n\r\n"
        await asyncio.sleep(1.0)  # the client stalls mid-upload
        yield b"\xff\xd8\xff" + b"0" * 10 + b"\r\n--b--\r\n"

    async def scenario(client, state):
        resp = await client.post("/api/analysis/faces", content=trickle(),
                                 headers={"Content-Type": "multipart/form-data; boundary=b"})
        assert resp.status_code == 408 and resp.json()["error"] == "upload_timeout"
        assert _slot_value(state) == 1
        ok = await client.post("/api/analysis/faces", files=_files())
        assert ok.status_code == 200

    asyncio.run(_run(make_app(), scenario))
    assert route.call_count == 1  # only the good request reached the model


def test_slot_released_after_every_kind_of_failure(make_app, frigate, settings):
    settings.analysis_concurrency = 1
    settings.analysis_max_image_bytes = 1000
    route = frigate.post(f"{INFERENCE}/v1/detect")
    route.side_effect = [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(503, json={"error": "model_unavailable", "detail": "not loaded"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=NO_FACES),
    ]

    async def scenario(client, state):
        codes = []
        for data in (b"GIF89a....", b"", b"\xff\xd8\xff" + b"0" * 5000):  # rejected before inference
            codes.append((await client.post("/api/analysis/faces", files=_files(data))).status_code)
        for _ in range(4):  # inference failures
            codes.append((await client.post("/api/analysis/faces", files=_files())).status_code)
        assert codes == [415, 400, 413, 503, 503, 503, 502]
        assert _slot_value(state) == 1
        final = await client.post("/api/analysis/faces", files=_files())
        assert final.status_code == 200  # the single slot is still usable

    asyncio.run(_run(make_app(), scenario))


def test_slot_released_when_client_cancels(make_app, frigate, settings):
    settings.analysis_concurrency = 1
    started = asyncio.Event()

    async def hanging_detect(request):
        started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json=NO_FACES)

    frigate.post(f"{INFERENCE}/v1/detect").mock(side_effect=hanging_detect)

    async def scenario(client, state):
        task = asyncio.create_task(client.post("/api/analysis/faces", files=_files()))
        await asyncio.wait_for(started.wait(), 5)
        assert _slot_value(state) == 0  # the slot is held during inference
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        assert _slot_value(state) == 1

    asyncio.run(_run(make_app(), scenario))


def test_oversized_declared_length_rejected_without_taking_a_slot(make_app, frigate, settings):
    settings.analysis_concurrency = 1
    settings.analysis_queue_timeout_seconds = 0.1
    frigate.post(f"{INFERENCE}/v1/detect").respond(200, json=NO_FACES)

    async def scenario(client, state):
        await state.analysis_slots.acquire()  # every slot busy
        try:
            resp = await client.post("/api/analysis/faces", content=b"x",
                                     headers={"Content-Type": "multipart/form-data; boundary=b",
                                              "Content-Length": str(50 * 1024 * 1024)})
            assert resp.status_code == 413  # answered at once, not queued behind the busy slot
        finally:
            state.analysis_slots.release()

    asyncio.run(_run(make_app(), scenario))
