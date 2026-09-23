FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common.py db.py schema.sql dispatcher.py ./
COPY static ./static
CMD ["sh", "-c", "uvicorn dispatcher:app --host 0.0.0.0 --port ${PORT:-8000}"]
