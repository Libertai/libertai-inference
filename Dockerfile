FROM python:3.12

WORKDIR /app

RUN pip install poetry

COPY ./pyproject.toml ./poetry.lock ./

RUN poetry install

COPY . .

CMD ["./docker-entrypoint.sh"]
