#!/bin/sh
# Migrate (serialized across replicas via a PG advisory lock), then serve.
set -e
python -m scripts.migrate
exec python -m fastapi run src/main.py --host 0.0.0.0
