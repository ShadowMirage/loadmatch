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

echo "Starting application..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000