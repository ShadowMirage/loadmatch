#!/bin/bash
set -e

echo "Waiting for PostgreSQL..."

until pg_isready -h db -U postgres -d loadmatch -q; do
  echo "Database unavailable - sleeping"
  sleep 2
done

echo "Database is ready."

echo "Running Alembic migrations..."

if alembic upgrade head; then
    echo "Migrations complete."
else
    echo "Migration failed!"
    exit 1
fi

echo "Starting application with Gunicorn (4 workers)..."
exec gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  --workers 4 \
  --bind 0.0.0.0:8000 \
  --timeout 150 \
  --log-level info