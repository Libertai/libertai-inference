FROM python:3.12

WORKDIR /app

# WeasyPrint needs Pango >=1.44 at runtime; DejaVu is the invoice template font.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install poetry

COPY ./pyproject.toml ./poetry.lock ./

RUN poetry install

COPY . .

CMD ["./docker-entrypoint.sh"]
