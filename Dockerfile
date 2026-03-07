FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install all system dependencies in ONE layer
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    postgresql-client \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Normalize line endings and make executable
RUN dos2unix start.sh && chmod +x start.sh

CMD ["./start.sh"]