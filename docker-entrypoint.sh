#!/bin/sh
# Migrate (serialized across replicas via a PG advisory lock), then serve.
set -e
poetry run python -m scripts.migrate
exec poetry run fastapi run src/main.py --host 0.0.0.0
