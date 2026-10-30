# Licences of dependencies, models and test data

Collected on 2026-10-30 from the installed packages' own metadata (`License-Expression`, `License` or the licence classifier; npm `license` fields), for the packages pinned in `backend/constraints.txt`, `inference/constraints.txt` and `frontend/package.json`. Exact versions are in those files and `frontend/package-lock.json`. This is an engineering inventory, not legal advice; check the licence texts before redistributing the product.

## Face models (downloaded at image build, pinned by SHA-256)
| Model | Licence | Source | Use |
|---|---|---|---|
| YuNet `face_detection_yunet_2023mar.onnx` | MIT | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | Face detection |
| SFace `face_recognition_sface_2021dec.onnx` | Apache-2.0 | [OpenCV Zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) | Face embeddings for recognition |

InsightFace's pretrained models were deliberately not used: their weights are licensed for non-commercial research only (ADR-0001).

## Test photographs
Four official NASA portraits, public domain in the United States; sources and changes in [inference/tests/data/public_domain/README.md](../inference/tests/data/public_domain/README.md). Used only by automated tests.

## Python: backend (`ai_surveillance`)
| Licence | Packages |
|---|---|
| MIT | alembic, annotated-doc, annotated-types, anyio, argon2-cffi, argon2-cffi-bindings, fastapi, h11, httptools, Mako, pydantic, pydantic-core, pydantic-settings, PyYAML, SQLAlchemy, typing-inspection, watchfiles |
| BSD-3-Clause | click, httpcore, httpx, idna, MarkupSafe, pycparser, python-dotenv, starlette, uvicorn, websockets |
| BSD (classifier only) | colorama (Windows only) |
| Apache-2.0 | opentelemetry-api, python-multipart |
| Apache-2.0 OR BSD-3-Clause | cryptography |
| Apache-2.0 OR BSD-2-Clause | packaging |
| MIT-0 | cffi |
| MPL-2.0 | certifi (CA bundle) |
| PSF-2.0 | typing-extensions |
| Development and test only | pytest, pluggy, iniconfig (MIT), Pygments (BSD-2-Clause), respx (BSD-3-Clause), ruff (MIT) |

## Python: inference service (`inference_service`)
| Licence | Packages |
|---|---|
| Apache-2.0 | opencv-python-headless (bundles OpenCV, Apache-2.0, and third-party libraries listed in the wheel's own notices) |
| BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | numpy |
| MIT-CMU | pillow |
| Same as the backend | fastapi, starlette, uvicorn, pydantic, pydantic-settings, httpx and their dependencies above |

## Frontend (`frontend`)
| Licence | Packages |
|---|---|
| MIT | react, react-dom, react-router-dom |
| Build and test only | vite, vitest, @vitejs/plugin-react, jsdom, @testing-library/react, @testing-library/jest-dom, @types/react, @types/react-dom (MIT); typescript (Apache-2.0) |

Transitive npm packages are not listed here; `npm ls --all` on the build machine shows them.

## Container images (not built from this repository's code)
| Image | Licence (as published by the project) | Note |
|---|---|---|
| Frigate `ghcr.io/blakeblackshear/frigate:0.18.2` (and `-tensorrt`) | MIT (code) | "Frigate" is a trademark; it is not used in this product's branding (ADR-0001) |
| Caddy `caddy:2.8.4-alpine` (web) | Apache-2.0 | |
| mediamtx `bluenviron/mediamtx:1.9.3-ffmpeg` (test stream only) | MIT (mediamtx) | The `-ffmpeg` variant includes FFmpeg, whose licence (LGPL or GPL) depends on how that image built it; **not verified here**. Only used for the optional synthetic test stream |
| `python:3.12-slim` (api, inference), `node:20.18-alpine` (frontend build stage only) | Python: PSF; Node.js: MIT; plus the distributions' own packages | Base images |

Not verified: transitive npm licences, the FFmpeg build inside the Frigate and mediamtx images, and the third-party notices bundled in the OpenCV wheel.
