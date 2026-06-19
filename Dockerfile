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
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
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
    tini \
    && rm -rf /var/lib/apt/lists/*

RUN playwright install chromium

COPY . .

# Run under tini (PID 1) so it reaps zombie Chromium child processes. Without an
# init, repeated headless-browser launches leave zombies that pile up over time
# until the container hits its process/thread limit ("can't start new thread").
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]
