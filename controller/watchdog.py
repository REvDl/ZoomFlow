"""
Два независимых сторожа:

1. DockerEventWatcher — слушает события docker.sock (die/oom) для контейнера core.
   docker-py даёт только блокирующий генератор событий, поэтому крутим его
   в отдельном потоке через run_in_executor и прокидываем результаты в asyncio
   через thread-safe очередь.

2. HeartbeatWatcher — раз в N секунд проверяет TTL-ключ core:heartbeat.
   Если он протух, а до этого core считался "живым" — шлём алерт один раз
   (не спамим на каждой итерации, пока heartbeat не восстановится).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Callable, Awaitable

import docker
import redis.asyncio as aioredis

logger = logging.getLogger("watchdog")

HEARTBEAT_KEY = "core:heartbeat"

AlertSender = Callable[[str], Awaitable[None]]


class DockerEventWatcher:
    def __init__(self, container_name: str, alert_sender: AlertSender):
        self.container_name = container_name
        self.alert_sender = alert_sender
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._client = docker.from_env()

    def _blocking_listen(self):
        """Работает в отдельном потоке. Слушает события die/oom для нужного контейнера."""
        try:
            for event in self._client.events(decode=True, filters={"container": self.container_name}):
                self._queue.put(event)
        except Exception:
            logger.exception("DockerEventWatcher: поток событий упал, требуется рестарт controller")

    async def run(self):
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._blocking_listen)

        while True:
            try:
                event = await loop.run_in_executor(None, self._queue.get, True, 1.0)
            except queue.Empty:
                await asyncio.sleep(0)
                continue

            action = event.get("Action")
            if action not in ("die", "oom"):
                continue

            attrs = event.get("Actor", {}).get("Attributes", {})
            exit_code = attrs.get("exitCode")

            if action == "oom" or exit_code == "137":
                text = (
                    f"⚠️ core упал по OOM (Out Of Memory), exit code 137.\n"
                    f"Контейнеру не хватило памяти (лимит mem_limit в docker-compose.yml). "
                    f"Автоматического рестарта нет (restart: no) — запусти вручную: "
                    f"`docker compose up -d core`."
                )
            elif exit_code not in (None, "0"):
                text = (
                    f"🔴 core упал, exit code {exit_code}.\n"
                    f"Возможные причины: ошибка валидации config.json, необработанное "
                    f"исключение в main loop. Проверь логи: `docker compose logs core --tail 100`."
                )
            else:
                continue

            await self.alert_sender(text)


class HeartbeatWatcher:
    def __init__(self, redis_client: aioredis.Redis, alert_sender: AlertSender, check_interval: int = 10):
        self.redis = redis_client
        self.alert_sender = alert_sender
        self.check_interval = check_interval
        self._was_alive = True
        self._alert_sent = False

    async def run(self):
        while True:
            alive = bool(await self.redis.exists(HEARTBEAT_KEY))

            if not alive and self._was_alive and not self._alert_sent:
                await self.alert_sender(
                    "🔴 core не обновляет heartbeat (ключ core:heartbeat истёк). "
                    "Похоже, процесс завис (например, заблокирован в waiting room без "
                    "параллельной таски, либо завис на сетевом запросе). Проверь "
                    "`docker compose logs core --tail 100` и при необходимости "
                    "`docker compose restart core`."
                )
                self._alert_sent = True

            if alive:
                self._alert_sent = False

            self._was_alive = alive
            await asyncio.sleep(self.check_interval)
