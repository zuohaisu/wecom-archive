FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates ffmpeg libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

RUN ffmpeg -version >/dev/null

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app \
    && mkdir -p /var/lib/wecom-archive/media /var/lib/wecom-archive/run \
    && chown -R app:app /var/lib/wecom-archive

WORKDIR /app/backend

COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=app:app backend/app ./app
COPY --chown=app:app backend/alembic ./alembic
COPY --chown=app:app backend/alembic.ini ./alembic.ini
COPY --chown=app:app backend/scripts ./scripts
COPY --chown=app:app docs/ARCHITECTURE.md /app/docs/ARCHITECTURE.md
COPY --chown=app:app docs/kb/ /app/docs/kb/

USER 10001:10001
EXPOSE 8035
