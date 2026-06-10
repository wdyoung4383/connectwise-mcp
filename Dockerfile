FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home appuser
USER appuser

# App Platform routes to the container port; bind beyond loopback here only.
ENV CW_MCP_HOST=0.0.0.0 \
    CW_MCP_PORT=8080
EXPOSE 8080

CMD ["connectwise-mcp"]
