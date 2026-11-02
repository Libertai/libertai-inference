# syntax=docker/dockerfile:1

# --- Builder: resolve and install the runtime dependency set into /opt/venv ------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

# Pinned to the Poetry that generated poetry.lock (see its header).
RUN pip install "poetry==2.5.1"

# Fixed absolute venv path: the shebangs poetry writes into /opt/venv/bin/* must still
# resolve once the venv is copied into the runtime stage.
RUN python -m venv /opt/venv

WORKDIR /app
COPY ./pyproject.toml ./poetry.lock ./

# Main group only: ruff and pytest are dev-only and must not ship. mypy still arrives
# through `sqlalchemy = { extras = ["mypy"] }`, a main dependency, so it stays until
# that extra is moved out of pyproject.toml — a separate change, not a Docker one.
RUN VIRTUAL_ENV=/opt/venv poetry install --only main --no-root \
    && rm -rf /root/.cache /root/.local/share/pypoetry

# --- Runtime: slim base + the venv; no Poetry, no compilers, no build cache -------
FROM python:3.13-slim AS runtime

WORKDIR /app

# Naive TIMESTAMP columns hold UTC and are compared against Python-side clocks; a non-UTC
# container would write local time into them.
ENV TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# WeasyPrint needs Pango >=1.44 at runtime (harfbuzz-subset0 for font subsetting);
# DejaVu is the invoice template font.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY . .

CMD ["./docker-entrypoint.sh"]
