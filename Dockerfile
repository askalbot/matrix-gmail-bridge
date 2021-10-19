# syntax=docker/dockerfile:1.2
FROM python:3.9
RUN mkdir -p /app
WORKDIR /app
RUN pip3 install poetry
COPY ./poetry.lock ./pyproject.toml /app/
RUN --mount=type=cache,target=/root/.cache/pip poetry config virtualenvs.create false && poetry install --no-ansi -n --no-dev

COPY ./app /app/app
CMD ["python3", "-m", "app.main"]
