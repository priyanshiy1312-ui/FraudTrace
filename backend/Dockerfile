# One image for both the API (uvicorn) and the MCP server (python -m mcp_server.server).
FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY shared ./shared
COPY mcp_server ./mcp_server
COPY backend ./backend
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN useradd -m app && chown -R app /app
USER app
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
