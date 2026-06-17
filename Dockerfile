FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium AND all its system libraries in one step (robust on slim images).
RUN playwright install --with-deps chromium

COPY . .

CMD ["python", "main.py"]
