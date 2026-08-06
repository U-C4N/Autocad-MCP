FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --shell /bin/bash mcp

COPY pyproject.toml README.md LICENSE ./
COPY server.py config.py security.py version.py ./
COPY backends/ ./backends/
COPY engineering/ ./engineering/
COPY discovery/ ./discovery/

RUN pip install --no-cache-dir -e .

USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import server, engineering.gear; print('ok')" || exit 1

CMD ["python", "server.py"]
