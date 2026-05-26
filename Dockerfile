FROM python:3.12-slim

WORKDIR /app

# Build deps for the package (PEP 517 via hatchling)
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .

# HTTP service defaults (override at runtime / in the stack)
ENV MCP_TRANSPORT=http \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8080 \
    DEPLOY_MODE=propose \
    BACKUP_DIR=/data/backups

EXPOSE 8080
VOLUME ["/data"]

CMD ["node-red-homeassistant-mcp"]
