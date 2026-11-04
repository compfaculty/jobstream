.PHONY: setup services migrate run worker browser-worker beat test check
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
browser-worker:
	docker compose up -d browser-worker
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

.PHONY: infinity infinity-django infinity-smoke infinity-test infinity-check infinity-logs
infinity:
	uv run --env-file .env --no-project python scripts/run_infinity.py
infinity-django:
	AGENT_BACKEND=infinity uv run python manage.py runserver
infinity-smoke:
	cargo build --locked --manifest-path services/infinity-agent/Cargo.toml
	INFINITY_INTEGRATION=1 uv run pytest tests/test_infinity_integration.py::test_real_runtime_end_to_end -q -s
infinity-test:
	cargo build --locked --manifest-path services/infinity-agent/Cargo.toml
	cargo test --locked --manifest-path services/infinity-agent/Cargo.toml
	INFINITY_INTEGRATION=1 uv run pytest tests/test_evaluations.py tests/test_infinity_integration.py -q
infinity-check:
	cargo fmt --manifest-path services/infinity-agent/Cargo.toml --check
	cargo clippy --locked --manifest-path services/infinity-agent/Cargo.toml --all-targets -- -D warnings
infinity-logs:
	uv run python manage.py agent_executions $(ARGS)

.PHONY: infinity-benchmark
infinity-benchmark:
	uv run python manage.py benchmark_evaluations --iterations 1000
	$(MAKE) infinity-smoke
