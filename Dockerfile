FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY banhammerbot ./banhammerbot
RUN uv sync --frozen --no-dev \
    && useradd --uid 10001 --create-home bot \
    && mkdir /app/data \
    && chown bot:bot /app/data
USER bot
CMD ["/app/.venv/bin/banhammerbot"]
