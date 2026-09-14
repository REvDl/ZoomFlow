import json
import logging
import os
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message
import redis.asyncio as aioredis

logger = logging.getLogger("bot")

ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])

STATUS_KEY = "core:status"
HEARTBEAT_KEY = "core:heartbeat"
COMMANDS_KEY = "core:commands"
SCHEDULE_KEY = "core:schedule_today"

STATUS_LABELS = {
    "not_today": "— не сегодня",
    "disabled": "🚫 отключена",
    "pending": "⏳ ожидает",
    "in_meeting": "🟢 идёт сейчас",
    "done": "✅ завершена",
    "failed": "❌ не удалось подключиться",
}


def build_router(redis_client: aioredis.Redis) -> Router:
    router = Router()

    @router.message.middleware()
    async def allowed_chat_only(handler, event: Message, data):
        if event.chat.id != ALLOWED_CHAT_ID:
            logger.warning("Игнорирую сообщение из чужого чата %s", event.chat.id)
            return
        return await handler(event, data)

    @router.message(Command("status"))
    async def cmd_status(message: Message):
        raw_status = await redis_client.get(STATUS_KEY)
        raw_schedule = await redis_client.get(SCHEDULE_KEY)
        hb_alive = await redis_client.exists(HEARTBEAT_KEY)

        lines = []
        lines.append("🟢 core живой (heartbeat в норме)" if hb_alive else "🔴 core НЕ отвечает (heartbeat истёк)")

        if raw_status:
            status = json.loads(raw_status)
            lines.append(f"Текущее состояние: {status.get('state', '?')}")

        if raw_schedule:
            schedule = json.loads(raw_schedule)
            lines.append("")
            lines.append("Расписание на сегодня:")
            for entry in schedule:
                label = STATUS_LABELS.get(entry["status"], entry["status"])
                lines.append(f"  #{entry['index']} {entry['start']}-{entry['end']} {entry['name']}: {label}")
        else:
            lines.append("Расписание на сегодня пока не сформировано.")

        await message.answer("\n".join(lines))

    @router.message(Command("stop"))
    async def cmd_stop(message: Message):
        await redis_client.lpush(COMMANDS_KEY, json.dumps({"action": "stop"}))
        await message.answer("Команда отправлена: выхожу из текущей конференции.")

    @router.message(Command("disable"))
    async def cmd_disable(message: Message):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await message.answer("Использование: /disable <N> — где N это индекс пары из /status")
            return
        idx = int(parts[1].strip())
        await redis_client.lpush(COMMANDS_KEY, json.dumps({"action": "disable", "pair_index": idx}))
        await message.answer(f"Команда отправлена: пара #{idx} будет отключена на сегодня.")

    return router


def build_bot_and_dispatcher(redis_client: aioredis.Redis) -> tuple[Bot, Dispatcher]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(build_router(redis_client))
    return bot, dp
