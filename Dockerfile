# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.14.8-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=0 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-editable
COPY src ./src
COPY LICENSE ./LICENSE
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION
ARG VCS_REF
LABEL org.opencontainers.image.title="romm-mcp" \
      org.opencontainers.image.description="Compact RomM tools over stdio and Streamable HTTP" \
      org.opencontainers.image.source="https://github.com/sharkusmanch/romm-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home app
COPY --from=build /app/.venv /app/.venv
COPY LICENSE /app/LICENSE
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["romm-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
