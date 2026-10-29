FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libasound2 ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py providers.py streaming.py ./
RUN useradd --create-home app
USER app
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --ws-max-size 65536"]
