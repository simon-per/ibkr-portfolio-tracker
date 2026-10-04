FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .

# The database is not baked into the image: docker-compose mounts backend/data/ at
# /app/data and sets DATABASE_URL to the file inside it.

EXPOSE 8000

# app.db_guard runs FIRST because alembic creates a missing database file, and an app
# serving a new, empty database looks like an account that lost everything (see its
# docstring). It refuses unless the file exists and is non-empty, or ALLOW_NEW_DATABASE=1.
CMD ["sh", "-c", "python -m app.db_guard && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
