# Repository Guidelines

## Project Structure & Module Organization

`banhammerbot/` contains the Python package. `__main__.py` manages startup, long polling, and shutdown; `api.py` handles Telegram and HTTP requests; `decision.py` builds Jev requests and validates responses. Moderation lives in `moderation.py`, private menus in `management.py`, and timed join mutes in `joins.py`. Shared permissions, configuration, and SQLite persistence belong in `permissions.py`, `config.py`, and `storage.py`.

`tests/test_*.py` mirrors these responsibilities. `pyproject.toml` defines dependencies and tooling; `uv.lock` pins dependencies. `Dockerfile` and `compose.yaml` package deployment. Runtime databases live in ignored `data/`; there is no static asset directory.

## Build, Test, and Development Commands

- `uv sync --frozen`: install runtime and development dependencies from the lockfile.
- `uv run banhammerbot --check`: validate Telegram credentials, webhook status, and configured group permissions without calling Jev.
- `uv run banhammerbot`: start the local polling worker.
- `uv run python -m unittest discover -s tests -v`: run the test suite.
- `uv run ruff check .`: check lint rules and import ordering.
- `uv run ruff format .`: format Python; use `--check` for validation.
- `uv build`: build distribution artifacts in `dist/`.
- `docker compose up -d --build`: build and run the `banhammerbot` service; inspect it with `docker compose logs -f banhammerbot`.

## Coding Style & Naming Conventions

Target Python 3.11+, use four-space indentation, and follow Ruff's 100-character line limit. Use `snake_case` for modules/functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Annotate production interfaces. Keep network operations asynchronous and preserve cancellation during shutdown.

## Testing Guidelines

Tests use `unittest`, `IsolatedAsyncioTestCase`, and mocked Telegram/Jev responses; credentials are unnecessary. Name files `test_<area>.py` and methods `test_<behavior>`. Cover consequential behavior changes, especially permissions, dry-run enforcement, API failures, persistence migrations, and shutdown. No numeric coverage threshold is configured. Run relevant tests and Ruff checks before submitting.

## Commit & Pull Request Guidelines

History uses short imperative subjects, such as `Add per-group management menus and timed join mutes`; follow that convention. Keep commits focused. PR descriptions should explain the problem, resulting behavior, validation performed, and any configuration or database changes. Link related issues and include menu screenshots when useful for reviewing layout changes.

## Security & Configuration

Copy `.env.example` to `.env`; never commit credentials, databases, or raw user evidence. Keep logs sanitized. Start with dry run and remember saved group overrides take precedence. Run only one worker per bot token. Preserve admin/bot exclusions, fresh permission checks, and the rule that profile promotion alone cannot justify a ban.
