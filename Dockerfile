# L'image Playwright embarque déjà Chromium + toutes les libs système.
# La version doit correspondre à celle de requirements.txt (playwright==1.49.0).
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
CMD ["python", "-m", "bot.main"]
