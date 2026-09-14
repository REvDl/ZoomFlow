"""
Автоматизация входа/выхода из Zoom-конференции через Zoom Web Client
(без установки десктопного приложения — избегаем deep-link на "Open Zoom App").

ВАЖНО: Zoom регулярно меняет вёрстку веб-клиента. Селекторы ниже —
рабочая отправная точка, а не гарантия "навечно". Если Zoom изменит
DOM, потребуется поправить CSS-селекторы в этом файле.

Публичный контракт:
    session = ZoomAutomation(pair, heartbeat_cb=..., stop_event=...)
    result = await session.run()   # -> "in_meeting_left" | "failed_waiting_room" | "failed_error" | "stopped"
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

logger = logging.getLogger("zoom_automation")

WAITING_ROOM_MARKERS = [
    "text=Please wait for the host",
    "text=waiting room",
    "text=Пожалуйста, подождите",
    "text=зал ожидания",
]

JOIN_FROM_BROWSER_LINK = "a#wc_bottom_btn, a:has-text('Join from your browser'), a:has-text('Присоединиться из браузера')"
NAME_INPUT = "#inputname, input[name='name']"
JOIN_BUTTON = "#joinBtn, button:has-text('Join'), button:has-text('Войти')"
IN_MEETING_MARKER = "#wc-container-right, div.footer__leave-btn-container, button:has-text('Leave'), button:has-text('Покинуть')"
LEAVE_BUTTON = "button:has-text('Leave'), button:has-text('Покинуть')"
LEAVE_CONFIRM_BUTTON = "button:has-text('Leave Meeting'), button:has-text('Покинуть конференцию')"


class ZoomAutomation:
    def __init__(
        self,
        pair: dict,
        heartbeat_cb: Optional[Callable[[], Awaitable[None]]] = None,
        stop_event: Optional[asyncio.Event] = None,
        waiting_room_timeout: int = 600,
    ):
        self.pair = pair
        self.heartbeat_cb = heartbeat_cb
        self.stop_event = stop_event or asyncio.Event()
        self.waiting_room_timeout = waiting_room_timeout
        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Optional[Page] = None

    async def _heartbeat_loop(self):
        """Крутится параллельно долгим ожиданиям, чтобы core:heartbeat не протухал."""
        while True:
            if self.heartbeat_cb:
                try:
                    await self.heartbeat_cb()
                except Exception:
                    logger.exception("heartbeat_cb упал")
            await asyncio.sleep(5)

    async def _launch(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--use-fake-ui-for-media-stream",  # авто-отклонение/эмуляция запроса камеры/микро
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._context = await self._browser.new_context(
            permissions=[],  # НЕ выдаём разрешения на камеру/микрофон
            viewport={"width": 1280, "height": 800},
        )
        self._page = await self._context.new_page()

    async def _join_flow(self) -> None:
        page = self._page
        assert page is not None
        await page.goto(self.pair["url"], wait_until="domcontentloaded", timeout=30000)

        # Пропускаем экран "Open Zoom Meetings?" и жмём "Join from your browser"
        try:
            await page.click(JOIN_FROM_BROWSER_LINK, timeout=15000)
        except PWTimeout:
            logger.info("Ссылка 'join from browser' не найдена — возможно, уже на форме имени")

        await page.wait_for_selector(NAME_INPUT, timeout=20000)
        await page.fill(NAME_INPUT, self.pair["name"])
        await page.click(JOIN_BUTTON)

    async def _is_in_meeting(self) -> bool:
        try:
            await self._page.wait_for_selector(IN_MEETING_MARKER, timeout=2000)
            return True
        except PWTimeout:
            return False

    async def _is_waiting_room(self) -> bool:
        for marker in WAITING_ROOM_MARKERS:
            try:
                el = await self._page.query_selector(marker)
                if el:
                    return True
            except Exception:
                continue
        return False

    async def _mute_camera_and_mic(self):
        """Подстраховка: даже если Zoom спросил разрешения — явно жмём mute, если кнопки есть."""
        for label in ["Stop Video", "Mute", "Остановить видео", "Выключить звук"]:
            try:
                btn = await self._page.query_selector(f"button:has-text('{label}')")
                if btn:
                    await btn.click()
            except Exception:
                pass

    async def _leave(self):
        try:
            await self._page.click(LEAVE_BUTTON, timeout=5000)
            await self._page.click(LEAVE_CONFIRM_BUTTON, timeout=5000)
        except PWTimeout:
            logger.warning("Не удалось корректно нажать Leave — закрываю страницу принудительно")

    async def run(self) -> str:
        hb_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._launch()
            await self._join_flow()

            # Ждём admit из waiting room либо прямого попадания в конференцию
            elapsed = 0
            poll_interval = 5
            in_meeting = False
            while elapsed < self.waiting_room_timeout:
                if self.stop_event.is_set():
                    return "stopped"
                if await self._is_in_meeting():
                    in_meeting = True
                    break
                if not await self._is_waiting_room():
                    # ни waiting room, ни meeting — возможно, ошибка/редирект
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            if not in_meeting:
                logger.warning("Не впустили из waiting room за %s сек", self.waiting_room_timeout)
                return "failed_waiting_room"

            await self._mute_camera_and_mic()

            # Основное ожидание до конца пары / команды stop
            while not self.stop_event.is_set():
                if not await self._page.is_closed() and not await self._is_in_meeting():
                    # хост завершил конференцию раньше времени
                    logger.info("Конференция завершена хостом раньше расписания")
                    return "in_meeting_left"
                await asyncio.sleep(5)

            await self._leave()
            return "in_meeting_left" if self.stop_event.is_set() else "in_meeting_left"

        except Exception:
            logger.exception("Ошибка автоматизации Zoom")
            return "failed_error"
        finally:
            hb_task.cancel()
            await self._cleanup()

    async def _cleanup(self):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            logger.exception("Ошибка при закрытии браузера")
