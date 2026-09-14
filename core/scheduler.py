"""
State machine и вся работа с Redis для core.

Ключи Redis (см. ТЗ):
  core:status            HASH/JSON  — текущее состояние
  core:heartbeat          STRING    — тик с TTL
  core:last_heartbeat_ts  STRING    — тик без TTL (для расчёта downtime)
  core:commands           LIST      — команды от controller (RPOP)
  core:disabled_pairs     SET       — индексы отключенных сегодня пар
  core:schedule_today     STRING(JSON) — расписание на сегодня со статусами
  core:alerts             PUB/SUB   — алерты для Telegram (слушает controller)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, date
from typing import Optional

import redis.asyncio as aioredis

from zoom_automation import ZoomAutomation

logger = logging.getLogger("scheduler")

STATUS_KEY = "core:status"
HEARTBEAT_KEY = "core:heartbeat"
LAST_HEARTBEAT_KEY = "core:last_heartbeat_ts"
COMMANDS_KEY = "core:commands"
DISABLED_KEY = "core:disabled_pairs"
SCHEDULE_KEY = "core:schedule_today"
ALERTS_CHANNEL = "core:alerts"


class RedisState:
    def __init__(self, host: str, port: int, heartbeat_ttl: int):
        self.r = aioredis.Redis(host=host, port=port, decode_responses=True)
        self.heartbeat_ttl = heartbeat_ttl

    async def heartbeat(self):
        now = str(int(time.time()))
        await self.r.set(HEARTBEAT_KEY, now, ex=self.heartbeat_ttl)
        await self.r.set(LAST_HEARTBEAT_KEY, now)

    async def get_last_heartbeat_ts(self) -> Optional[int]:
        val = await self.r.get(LAST_HEARTBEAT_KEY)
        return int(val) if val is not None else None

    async def set_status(self, status: dict):
        await self.r.set(STATUS_KEY, json.dumps(status, ensure_ascii=False))

    async def get_disabled_pairs(self) -> set[int]:
        members = await self.r.smembers(DISABLED_KEY)
        return {int(m) for m in members}

    async def add_disabled_pair(self, idx: int):
        await self.r.sadd(DISABLED_KEY, idx)

    async def clear_disabled_pairs(self):
        await self.r.delete(DISABLED_KEY)

    async def pop_command(self) -> Optional[dict]:
        raw = await self.r.rpop(COMMANDS_KEY)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Некорректная команда в core:commands: %r", raw)
            return None

    async def set_schedule_today(self, schedule: list[dict]):
        await self.r.set(SCHEDULE_KEY, json.dumps(schedule, ensure_ascii=False))

    async def publish_alert(self, text: str):
        await self.r.publish(ALERTS_CHANNEL, text)


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _time_to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class Scheduler:
    def __init__(
        self,
        pairs: list[dict],
        redis_state: RedisState,
        poll_interval: int,
        waiting_room_timeout: int,
    ):
        self.pairs = pairs
        self.redis = redis_state
        self.poll_interval = poll_interval
        self.waiting_room_timeout = waiting_room_timeout

        self._today: date = date.today()
        self._today_outcomes: dict[int, str] = {}  # pair_index -> done/failed
        self._active_pair_idx: Optional[int] = None
        self._active_stop_event: Optional[asyncio.Event] = None
        self._active_task: Optional[asyncio.Task] = None

    # ---------- helpers ----------

    def _reset_day_if_needed(self, now: datetime):
        if now.date() != self._today:
            self._today = now.date()
            self._today_outcomes = {}
            asyncio.create_task(self.redis.clear_disabled_pairs())

    def _pair_matches_today(self, pair: dict, now: datetime) -> bool:
        return now.isoweekday() in pair["days"]

    async def find_active_pair(self, now: datetime) -> Optional[dict]:
        disabled = await self.redis.get_disabled_pairs()
        nowmin = _now_minutes(now)
        for pair in self.pairs:
            if pair["index"] in disabled:
                continue
            if not self._pair_matches_today(pair, now):
                continue
            start = _time_to_minutes(pair["start"])
            end = _time_to_minutes(pair["end"])
            if start <= nowmin <= end:
                return pair
        return None

    async def build_schedule_today(self, now: datetime) -> list[dict]:
        disabled = await self.redis.get_disabled_pairs()
        nowmin = _now_minutes(now)
        schedule = []
        for pair in self.pairs:
            entry = {"index": pair["index"], "name": pair["name"], "start": pair["start"], "end": pair["end"]}
            if not self._pair_matches_today(pair, now):
                entry["status"] = "not_today"
            elif pair["index"] in disabled:
                entry["status"] = "disabled"
            elif pair["index"] in self._today_outcomes:
                entry["status"] = self._today_outcomes[pair["index"]]
            elif pair["index"] == self._active_pair_idx:
                entry["status"] = "in_meeting"
            else:
                start = _time_to_minutes(pair["start"])
                end = _time_to_minutes(pair["end"])
                entry["status"] = "pending" if nowmin < end else "done"
            schedule.append(entry)
        return schedule

    # ---------- startup: edge case Б (перезапуск посреди пары) ----------

    async def startup_check(self) -> Optional[str]:
        """Возвращает текст алерта, если рестарт произошёл посреди активной пары."""
        now = datetime.now()
        pair = await self.find_active_pair(now)
        if pair is None:
            return None

        last_ts = await self.redis.get_last_heartbeat_ts()
        if last_ts is None:
            downtime_min = 0
        else:
            downtime_min = max(0, round((time.time() - last_ts) / 60))

        text = (
            f'Перезапуск во время пары "{pair["name"]}" ({pair["start"]}-{pair["end"]}). '
            f"Бот отсутствовал {downtime_min} мин. Дозахожу сейчас."
        )
        await self.redis.publish_alert(text)
        return text

    # ---------- commands ----------

    async def process_commands(self):
        while True:
            cmd = await self.redis.pop_command()
            if cmd is None:
                break
            action = cmd.get("action")
            if action == "stop":
                if self._active_stop_event is not None:
                    self._active_stop_event.set()
                    await self.redis.publish_alert("Получена команда /stop — выхожу из текущей конференции.")
                else:
                    await self.redis.publish_alert("Команда /stop получена, но сейчас нет активной конференции.")
            elif action == "disable":
                idx = cmd.get("pair_index")
                if idx is None:
                    continue
                await self.redis.add_disabled_pair(int(idx))
                if self._active_pair_idx == idx and self._active_stop_event is not None:
                    self._active_stop_event.set()
                await self.redis.publish_alert(f"Пара #{idx} отключена на сегодня.")
            else:
                logger.warning("Неизвестная команда: %r", cmd)

    # ---------- main loop ----------

    async def run_forever(self):
        while True:
            now = datetime.now()
            self._reset_day_if_needed(now)

            await self.process_commands()

            schedule = await self.build_schedule_today(now)
            await self.redis.set_schedule_today(schedule)

            if self._active_task is None:
                pair = await self.find_active_pair(now)
                if pair is not None and pair["index"] not in self._today_outcomes:
                    await self._start_session(pair)
            else:
                # если время пары истекло, а сессия почему-то ещё не остановилась сама — подстрахуемся
                nowmin = _now_minutes(now)
                active_pair = self.pairs[self._active_pair_idx] if self._active_pair_idx is not None else None
                if active_pair and nowmin > _time_to_minutes(active_pair["end"]) + 2:
                    self._active_stop_event.set()

            await self.redis.heartbeat()
            await asyncio.sleep(self.poll_interval)

    async def _start_session(self, pair: dict):
        self._active_pair_idx = pair["index"]
        self._active_stop_event = asyncio.Event()

        await self.redis.set_status({"state": "joining", "pair_index": pair["index"], "name": pair["name"]})
        await self.redis.publish_alert(f'Подключаюсь к паре "{pair["name"]}" ({pair["start"]}-{pair["end"]})...')

        automation = ZoomAutomation(
            pair,
            heartbeat_cb=self.redis.heartbeat,
            stop_event=self._active_stop_event,
            waiting_room_timeout=self.waiting_room_timeout,
        )

        async def _run_and_finalize():
            await self.redis.set_status({"state": "in_meeting", "pair_index": pair["index"], "name": pair["name"]})
            result = await automation.run()

            if result == "failed_waiting_room":
                self._today_outcomes[pair["index"]] = "failed"
                await self.redis.publish_alert(
                    f'Не впустили из зала ожидания на пару "{pair["name"]}" за '
                    f"{self.waiting_room_timeout // 60} мин. Пара помечена как failed."
                )
            elif result == "failed_error":
                self._today_outcomes[pair["index"]] = "failed"
                await self.redis.publish_alert(
                    f'Ошибка автоматизации при входе на пару "{pair["name"]}". Смотри логи core.'
                )
            else:  # in_meeting_left / stopped
                self._today_outcomes[pair["index"]] = "done"
                await self.redis.publish_alert(f'Пара "{pair["name"]}" завершена, бот вышел из конференции.')

            await self.redis.set_status({"state": "idle"})
            self._active_pair_idx = None
            self._active_stop_event = None
            self._active_task = None

        self._active_task = asyncio.create_task(_run_and_finalize())
