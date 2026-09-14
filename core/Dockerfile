FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Браузеры уже установлены в базовом образе, но на случай смены версии
# requirements.txt — подстрахуемся (no-op, если уже актуальны).
RUN python -m playwright install --with-deps chromium

COPY . .

CMD ["python", "main.py"]
