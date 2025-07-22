# Use a slim Python image
FROM python:3.9-slim

# Install system deps (ffmpeg & yt‑dlp)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    pip install yt-dlp && \
    rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy Python requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your code
COPY . .

# Tell Flask to listen on all interfaces
ENV FLASK_RUN_HOST=0.0.0.0

# Production server
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
