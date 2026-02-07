# Canvas Lens

Canvas Lens is a Django app for staff to:

- connect to Canvas with a personal access token,
- sync courses and published assignments,
- filter assignments quickly,
- generate downloadable submissions reports in the background.

The app runs with PostgreSQL, Redis, Celery worker, and Celery beat.

## What It Does

- Staff-only access (authenticated users must also have `is_staff=True`).
- Canvas token validation and storage per staff user.
- Course sync from:
  - enrolled courses, or
  - a selected admin account.
- Assignment filtering by:
  - course/course name (supports `AND` / `OR` expressions),
  - assignment type,
  - assignment name,
  - enrollment status,
  - date range.
- Background submissions report generation with live progress.
- CSV download of completed reports.
- Report lifecycle:
  - manual delete per report,
  - automatic cleanup of old reports (older than 1 hour, every 5 minutes),
  - pending/running reports are not auto-deleted.
- CSV report includes dynamic group-set columns (Canvas group categories):
  - one column per group set,
  - each value is the student group name in that set (if any).

## Stack

- Python 3.11
- Django 5
- PostgreSQL (pgvector image used in `docker-compose.yml`)
- Redis
- Celery worker + Celery beat
- Gunicorn

## Services (Docker Compose)

- `web`: Django app (Gunicorn), exposed on `http://localhost:8010`
- `worker`: Celery worker for background jobs
- `scheduler`: Celery beat for scheduled jobs
- `db`: PostgreSQL
- `redis`: Redis broker/result backend

## Prerequisites

- Docker + Docker Compose

## Environment Variables

Create `.env` in project root.

Minimum required:

```env
DJANGO_SECRET_KEY=change-me
DJANGO_DEBUG=1
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DATABASE_URL=postgresql://postgres:postgres@db:5432/canvaslens
REDIS_URL=redis://redis:6379/0
```

Notes:

- The repository’s `.env.example` currently contains legacy values from an older project; use the values above for Canvas Lens.
- Canvas base URL is currently configured in code as:
  - `https://canvas.liverpool.ac.uk`

## Quick Start

1. Create `.env`

```bash
cp .env.example .env
```

Then update the values to match the `Environment Variables` section above.

2. Build and start all services

```bash
docker compose up -d --build
```

3. Run migrations

```bash
docker compose exec web python manage.py migrate
```

4. Create a staff user

```bash
docker compose exec web python manage.py createsuperuser
```

5. Open app

- `http://localhost:8010/login/`

## First-Run Workflow

1. Log in as a staff user.
2. Go to **Admin Dashboard**.
3. Paste Canvas token and save (token is validated).
4. Optional: choose sync source (enrolled vs admin account).
5. Trigger **Sync All** (or **Sync Existing**).
6. Open **Canvas Assignments** and apply filters.
7. Click **Create Submissions Report**.
8. Monitor progress and download CSV when complete.

## Report Notes

- Report creation is asynchronous (Celery task).
- UI updates progress without full-page refresh.
- Reports table updates automatically when a report finishes.
- CSV contains base submission columns plus dynamic group-set columns.

## Useful Commands

Run Django checks:

```bash
docker compose exec web python manage.py check
```

Tail logs:

```bash
docker compose logs -f web worker scheduler
```

Restart app services:

```bash
docker compose up -d --build web worker scheduler
```

## Troubleshooting

- `Cannot filter a query once a slice has been taken`
  - fixed in current code; ensure containers are rebuilt after pulling changes.
- Report not running:
  - check `worker` logs.
- Scheduled cleanup not running:
  - check `scheduler` logs.
- Canvas API errors:
  - verify token and Canvas permissions.

## Security / Access

- Non-authenticated users are redirected to login.
- Non-staff authenticated users receive `403 Staff access required.`

