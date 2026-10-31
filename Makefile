.PHONY: setup services migrate run worker beat test check
setup:
	uv sync --frozen
services:
	docker compose up -d db redis storage
migrate:
	uv run python manage.py migrate
run:
	uv run python manage.py runserver
worker:
	uv run celery -A config worker --loglevel=INFO --queues=celery,ingestion,preparation,delivery --concurrency=2
beat:
	uv run celery -A config beat --loglevel=INFO --schedule=/tmp/jobstream-celerybeat
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy jobs/services/authorization.py jobs/services/submissions.py
	uv run python manage.py check
	uv run python manage.py makemigrations --check --dry-run
	test -f uv.lock
test:
	uv run pytest -m 'not browser' --cov=jobs --cov-report=term-missing --cov-report=json
	uv run python scripts/check_coverage.py
