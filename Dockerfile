FROM python:3.12-slim

# Set timezone to Boston (US Eastern)
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Install ALL system deps needed for Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 libx11-xcb1 \
    libxfixes3 libxext6 libx11-6 libxcb1 libxdamage1 libexpat1 \
    libglib2.0-0 libnspr4 libdbus-1-3 libatspi2.0-0 \
    fonts-liberation fonts-noto-color-emoji wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "120", "--workers", "2", "app:app"]
