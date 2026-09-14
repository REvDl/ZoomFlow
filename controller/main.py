import asyncio
import logging
import os

import redis.asyncio as aioredis

from bot import build_bot_and_dispatcher
from watchdog import DockerEventWatcher, HeartbeatWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
CORE_CONTAINER_NAME = os.environ.get("CORE_CONTAINER_NAME", "zoom_core")
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
ALERTS_CHANNEL = "core:alerts"


async def alerts_pubsub_forwarder(redis_client: aioredis.Redis, alert_sender):
    """Слушает core:alerts (алерты, публикуемые самим core) и пересылает в Telegram."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(ALERTS_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        await alert_sender(message["data"])


async def main():
    redis_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    await redis_client.ping()

    bot, dp = build_bot_and_dispatcher(redis_client)

    async def alert_sender(text: str):
        try:
            await bot.send_message(chat_id=ALLOWED_CHAT_ID, text=text)
        except Exception:
            logger.exception("Не удалось отправить алерт в Telegram")

    docker_watcher = DockerEventWatcher(CORE_CONTAINER_NAME, alert_sender)
    heartbeat_watcher = HeartbeatWatcher(redis_client, alert_sender)

    await asyncio.gather(
        dp.start_polling(bot),
        docker_watcher.run(),
        heartbeat_watcher.run(),
        alerts_pubsub_forwarder(redis_client, alert_sender),
    )


if __name__ == "__main__":
    asyncio.run(main())
