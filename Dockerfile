# Synology Container Manager / Docker Compose friendly image.
# Prefer headless OpenCV (no GUI libs) for NAS containers.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    YIDUN_HOST=0.0.0.0 \
    YIDUN_PORT=8765 \
    YIDUN_API_TOKEN=

WORKDIR /app

# OpenCV runtime libs commonly needed on slim images.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 \
      libgomp1 \
      libgl1 \
      curl \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

COPY requirements.txt ./
# requirements.txt pins opencv-python; NAS images should use headless instead.
RUN --mount=type=cache,target=/root/.cache/pip \
    grep -v '^opencv-python' requirements.txt > /tmp/requirements.nas.txt \
    && pip install -r /tmp/requirements.nas.txt \
    && pip install "opencv-python-headless>=4.8.0"

COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser tools ./tools
COPY --chown=appuser:appuser captures/.gitkeep ./captures/.gitkeep
COPY --chown=appuser:appuser results/.gitkeep ./results/.gitkeep

RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["python", "tools/nas_api_server.py"]
