"""Client for the internal inference service (inference/ in the repository)."""

import httpx

from .config import Settings


class InferenceUnavailable(Exception):
    """The inference service could not be reached or did not answer in time."""


class InferenceClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        token = settings.inference_api_token.get_secret_value() if settings.inference_api_token else ""
        self._client = httpx.AsyncClient(
            base_url=settings.inference_url.rstrip("/"),
            timeout=settings.inference_timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"} if token else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise InferenceUnavailable("The inference service did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise InferenceUnavailable(f"The inference service is unreachable ({type(exc).__name__})") from exc
        if resp.status_code == 401:
            raise InferenceUnavailable(
                "The inference service refused the api's token; INFERENCE_API_TOKEN must be the same "
                "for the api and inference containers"
            )
        return resp

    async def health(self) -> dict:
        resp = await self._send("GET", "/v1/health")
        if resp.status_code != 200:
            raise InferenceUnavailable(f"Inference health check returned HTTP {resp.status_code}")
        return resp.json()

    async def models(self) -> list[dict]:
        resp = await self._send("GET", "/v1/models")
        if resp.status_code != 200:
            raise InferenceUnavailable(f"Inference model list returned HTTP {resp.status_code}")
        return resp.json()

    async def detect(self, image: bytes, min_score: float | None, max_faces: int | None) -> httpx.Response:
        """Raw response: the caller maps the service's error codes."""
        params = {k: v for k, v in {"min_score": min_score, "max_faces": max_faces}.items() if v is not None}
        return await self._send("POST", "/v1/detect", content=image, params=params,
                                headers={"Content-Type": "application/octet-stream"})

    async def embed(self, image: bytes, min_score: float | None, max_faces: int | None,
                    crops: bool = False) -> httpx.Response:
        """Like detect, plus an SFace embedding per face. Raw response, as for detect."""
        params = {k: v for k, v in {"min_score": min_score, "max_faces": max_faces}.items() if v is not None}
        params["crops"] = "true" if crops else "false"
        return await self._send("POST", "/v1/embed", content=image, params=params,
                                headers={"Content-Type": "application/octet-stream"})
