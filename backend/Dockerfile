FROM python:3.12-slim

# ffprobe is used by `ai-surveillance probe` to verify RTSP streams.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml constraints.txt ./
COPY ai_surveillance ./ai_surveillance
# constraints.txt pins the exact versions the tests passed with.
RUN pip install --no-cache-dir -c constraints.txt .

RUN useradd --system --uid 10001 --home /data app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "--factory", "ai_surveillance.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
