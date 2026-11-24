FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXAM_DB_PATH=/data/exam.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /data

EXPOSE 5000
CMD ["python", "-m", "flask", "--app", "app.web", "run", "--host", "0.0.0.0", "--port", "5000"]
