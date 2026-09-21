FROM golang:1.26-bookworm AS wireproxy-builder

RUN CGO_ENABLED=0 go install github.com/windtf/wireproxy/cmd/wireproxy@v1.1.2

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MP4_VPN_AUTO=1 \
    MP4_VPN_MODE=wireproxy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tor privoxy \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=wireproxy-builder /go/bin/wireproxy /usr/local/bin/wireproxy
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY static ./static
COPY README.md ./

EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
