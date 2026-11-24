# One image for all app services (api, refresh, migrate); only the command differs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["python", "-m", "rates.api"]
