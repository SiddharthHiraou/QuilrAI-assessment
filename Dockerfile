FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 10001 tollgate && mkdir -p /data && chown tollgate /data

USER tollgate
# Every service in compose uses this image with a different command.
CMD ["python", "-m", "tollgate.mcp_gateway", "--host", "0.0.0.0"]
