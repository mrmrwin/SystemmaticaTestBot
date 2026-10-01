FROM python:3.12-slim

# tzdata нужен модулю zoneinfo: в slim-образе нет системной базы часовых поясов
RUN pip install --no-cache-dir tzdata==2025.2 \
    && useradd --create-home --uid 10001 bot \
    && mkdir /data && chown bot /data

WORKDIR /app
COPY core.py bot.py ./
COPY tests ./tests

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/bot.db
USER bot
VOLUME ["/data"]

CMD ["python", "bot.py"]
