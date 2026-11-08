# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# Copy project metadata and install dependencies first (better layer caching)
COPY pyproject.toml README.md LICENSE ./
COPY fartlek/ ./fartlek/

# The base install reads a Garmin account export and needs no Garmin login.
# The direct connection is optional and left out of the image by default:
#   docker build --build-arg CONNECT=true -t fartlek-mcp .
# adds the `connect` extra (garminconnect). Read the README warning first.
ARG CONNECT=false
RUN if [ "$CONNECT" = "true" ]; then uv pip install --system ".[connect]"; \
    else uv pip install --system .; fi

# The store (and tokens, with the direct connection) live on a volume, not in the image
ENV FARTLEK_HOME=/data
VOLUME /data

# The MCP server speaks stdio. To import an export, override the entrypoint:
#   docker run --rm -v "$PWD/fartlek-data:/data" -v "$PWD/export.zip:/export/export.zip:ro" \
#     --entrypoint fartlek fartlek-mcp import /export/export.zip
ENTRYPOINT ["fartlek-mcp"]
