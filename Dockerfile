# KIS Trading Agent — container image, tuned for Hugging Face Spaces (Docker SDK).
# Also runs as-is on Render / Fly.io / Railway / any Docker host.
FROM python:3.10-slim

# HF Spaces runs containers as a non-root user; follow their recommended pattern
# so the writable paths are owned by the runtime user (avoids permission errors).
RUN useradd -m -u 1000 user
USER user

ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Safe public defaults — override real values via the Space's Secrets, not here.
    KIS_ENV=mock \
    KIS_ALLOW_LIVE_ORDERS=false \
    DATA_DIR=/home/user/app/data \
    KIS_TOKEN_CACHE_DIR=/home/user/app/data/.tokens \
    PORT=8000

WORKDIR /home/user/app

# Install deps first for layer caching.
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# App code + assets (NOT data/ or .env — see .dockerignore).
COPY --chown=user src/ src/
COPY --chown=user static/ static/
COPY --chown=user templates/ templates/

# Fresh writable state dir; the app recreates JSON files with safe defaults.
RUN mkdir -p /home/user/app/data/.tokens

EXPOSE 8000

# Shell form so ${PORT} expands at runtime (HF/Render inject their own PORT).
CMD uvicorn trading_agent.main:app --app-dir src --host 0.0.0.0 --port ${PORT:-8000}
