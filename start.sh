#!/bin/bash
echo "Running database migrations..."
alembic upgrade head
echo "Starting bot..."
python -m app.main