FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DIGITAL_SOLUTIONS_ASSETS_DIR=/app/assets/Agents.Testing

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates git curl \
  && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY assets ./assets
COPY scripts ./scripts
COPY config.toml ./config.toml

RUN pip install --no-cache-dir .

ENV DIGITAL_SOLUTIONS_MCP_CONFIG_TOML=/app/config.toml

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)); assert data.get('status')=='ok'"

CMD ["digital-solutions-test-mcp"]
