FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright system dependencies manually to avoid missing font packages
# (ttf-unifont, ttf-ubuntu-font-family) in Debian Trixie, then install Chromium
# without --with-deps since the deps are already present.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libatk1.0-0 \
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

RUN playwright install chromium

COPY . .

CMD ["python", "main.py"]
