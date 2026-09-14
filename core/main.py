import asyncio
import logging
import os
import sys

from config_validator import ConfigValidationError, load_and_validate
from scheduler import RedisState, Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "10"))
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "25"))
WAITING_ROOM_TIMEOUT = int(os.environ.get("WAITING_ROOM_TIMEOUT", "600"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))


async def async_main(pairs):
    redis_state = RedisState(REDIS_HOST, REDIS_PORT, HEARTBEAT_TTL)

    # Проверяем доступность Redis перед стартом основного цикла
    try:
        await redis_state.r.ping()
    except Exception as exc:
        logger.error("Redis недоступен на %s:%s: %s", REDIS_HOST, REDIS_PORT, exc)
        sys.exit(1)

    scheduler = Scheduler(
        pairs=pairs,
        redis_state=redis_state,
        poll_interval=POLL_INTERVAL,
        waiting_room_timeout=WAITING_ROOM_TIMEOUT,
    )

    # Edge case Б: рестарт посреди пары -> алерт + немедленное дозаход
    # (дозаход произойдёт естественным путём в первой итерации run_forever,
    # т.к. find_active_pair сразу же найдёт активную пару).
    await scheduler.startup_check()

    await scheduler.run_forever()


def main():
    try:
        pairs = load_and_validate(CONFIG_PATH)
    except ConfigValidationError as e:
        logger.error("ОШИБКА ВАЛИДАЦИИ config.json: %s", e)
        sys.exit(1)

    logger.info("Конфигурация валидна: %d пар(ы)", len(pairs))

    try:
        asyncio.run(async_main(pairs))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
