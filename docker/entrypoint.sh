#!/usr/bin/env sh
set -eu

mkdir -p /app/data /app/logs /app/reports/daily /app/storage

exec bash /app/scripts/local_pipeline.sh "$@"
