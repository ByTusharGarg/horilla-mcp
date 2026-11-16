# Container image for deploying the MCP server over HTTP (Railway, Render, Fly.io, a VM...).
# Not needed for local use: locally, Claude runs the server over stdio.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --create-home mcp
USER mcp

ENV MCP_HOST=0.0.0.0 PORT=9000
EXPOSE 9000
# Needs HORILLA_URL, HORILLA_USERNAME, HORILLA_PASSWORD and MCP_AUTH_TOKEN at runtime.
CMD ["horilla-mcp", "--http"]
