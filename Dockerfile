FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies needed for SSL certificates, unzipping Xray, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/* \
    && echo "precedence ::ffff:0:0/96 100" >> /etc/gai.conf

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files (including GeoLite2-Country.mmdb, plans_config.json, update_banner.png)
COPY . .

# Run the unified Telegram bot
CMD ["python", "ConfigBot.py"]
