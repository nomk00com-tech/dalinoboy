FROM python:3.13-slim

# Install system dependencies required by Playwright's Chromium browser:
# - libnss3, libatk*, libcups, libdrm, libxkb*, libgbm, libasound — Chromium runtime libs
# - fonts-liberation — fallback fonts so pages render correctly
# - wget — used internally by playwright install-deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libpango-1.0-0 \
    libcairo2 \
    fonts-liberation \
    wget \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Chromium browser binary for the installed playwright version
RUN playwright install chromium

# Copy the rest of the application source
COPY . .

CMD ["python", "main.py"]
