FROM python:3.11-slim
WORKDIR /app
COPY seed/requirements.txt seed/requirements.txt
RUN pip install --no-cache-dir -r seed/requirements.txt
COPY shared ./shared
COPY seed ./seed
ENV PYTHONPATH=/app
CMD ["python", "-m", "seed.load"]
