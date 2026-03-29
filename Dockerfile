FROM python:3.11-slim

# ------------------------------------------------------------------
# Chromium system dependencies for Debian Bookworm (python:3.11-slim base).
# We install these manually because `playwright install-deps` falls back to
# an Ubuntu 20.04 package list on Debian and tries to install fonts that
# do not exist in Bookworm/Trixie repos.
# ------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libatspi2.0-0 \
    libexpat1 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxcb1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's bundled Chromium only (no install-deps)
RUN playwright install chromium

# Copy application source
COPY src/ ./src/

# Persistent volumes are mounted at runtime:
#   /app/user_data  – Chromium profile (login session)
#   /app/data       – SQLite seen-messages DB
VOLUME ["/app/user_data", "/app/data"]

ENV PYTHONUNBUFFERED=1 \
    HEADLESS=1 \
    DB_PATH=/app/data/hub.db

ENTRYPOINT ["python", "-m", "src.main"]
