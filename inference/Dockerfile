FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY inference_service ./inference_service
RUN pip install --no-cache-dir .

# Models are downloaded and checksum-verified at build time, so the running
# service needs no internet access. A mismatch fails the build.
ENV INFERENCE_MODELS_DIR=/models
RUN python -m inference_service.download --models-dir /models

RUN useradd --system --uid 10002 --no-create-home inference
USER inference
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import json,sys,urllib.request; sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4))['ready'] else 1)"

CMD ["uvicorn", "--factory", "inference_service.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
