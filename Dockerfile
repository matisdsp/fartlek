# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Copy project metadata and install dependencies first (better layer caching)
COPY pyproject.toml README.md LICENSE ./
COPY fartlek/ ./fartlek/

RUN uv pip install --system .

# Data and tokens live on a volume, not in the image
ENV FARTLEK_HOME=/data
VOLUME /data

# The MCP server speaks stdio
ENTRYPOINT ["fartlek-mcp"]
