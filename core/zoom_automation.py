"""
Автоматизация входа/выхода из Zoom-конференции через Zoom Web Client
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Optional

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

logger = logging.getLogger("zoom_automation")

WAITING_ROOM_MARKERS = [
    "text=Please wait for the host",
    "text=waiting room",
    "text=Пожалуйста, подождите",
    "text=зал ожидания",
    "text=Host will let you in soon",
]

MEETING_ENDED_MARKERS = [
    "text=This meeting has been ended by host",
    "text=Meeting has ended",
    "text=Встреча завершена",
    "text=конференция завершена организатором",
    "text=has ended",
    "text=Meeting ended by host",
]

NAME_INPUT = "#inputname, input[name='name'], input[placeholder*='Name'], input[placeholder*='имя'], #input-for-name"
JOIN_BUTTON = "#joinBtn, button:has-text('Join'), button:has-text('Войти'), button.preview-join-button"

IN_MEETING_MARKERS = [
    "#wc-container-right",
    "div.footer__leave-btn-container",
    "div.footer-button__button-handle",
    "button.footer-button__button-icon--leave",
    "button[aria-label*='Leave']",
    "button[aria-label*='Покинуть']",
]


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

        LOW_RAM_ARGS = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--disable-blink-features=AutomationControlled",
            "--no-zygote",
            "--renderer-process-limit=1",
            "--js-flags=--max-old-space-size=256",
            "--disable-infobars",
            "--window-size=1366,800",
        ]

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=LOW_RAM_ARGS,
            ignore_default_args=["--enable-automation"],
        )

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        )

        self._context = await self._browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1366, "height": 800},
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
            locale="en-US",
            timezone_id="Europe/Kyiv",
            permissions=["microphone"],
        )
        self._context.set_default_timeout(15000)
        self._context.set_default_navigation_timeout(20000)

        self._page = await self._context.new_page()

        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'uk'] });
        """)

        self._page.on("dialog", lambda dialog: asyncio.create_task(self._handle_dialog(dialog)))
        self._page.on("console", lambda msg: logger.info(f"[console:{msg.type}] {msg.text}"))
        self._page.on("pageerror", lambda err: logger.warning(f"[pageerror] {err}"))

    async def _handle_dialog(self, dialog):
        logger.info(f"[DIAG] Нативный диалог: type={dialog.type!r} message={dialog.message!r}")
        await dialog.accept()

    async def _human_click(self, locator_or_element) -> bool:
        """Реалистичный клик по элементу."""
        try:
            box = await locator_or_element.bounding_box()
            if not box:
                await locator_or_element.click()
                return True
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2

            await self._page.mouse.move(cx - 10, cy - 5)
            await asyncio.sleep(0.05)
            await self._page.mouse.move(cx, cy, steps=5)
            await asyncio.sleep(0.1)
            await self._page.mouse.down()
            await asyncio.sleep(0.08)
            await self._page.mouse.up()
            await asyncio.sleep(0.15)
            return True
        except Exception as e:
            logger.warning(f"Ошибка human_click: {e}, пробуем обычный click()")
            try:
                await locator_or_element.click()
                return True
            except Exception:
                return False

    async def _join_flow(self) -> None:
        page = self._page
        assert page is not None

        raw_url = self.pair["url"]

        meeting_id_match = re.search(r"/j/(\d+)", raw_url)
        if meeting_id_match:
            meeting_id = meeting_id_match.group(1)
            pwd_param = ""
            if "pwd=" in raw_url:
                pwd_param = "&" + raw_url.split("?")[-1]

            target_url = f"https://zoom.us/wc/{meeting_id}/join?prefer=1{pwd_param}"
            logger.info(f"Сформирована прямая ссылка Web Client: {target_url}")
        else:
            target_url = raw_url

        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        name_field = await page.wait_for_selector(NAME_INPUT, state="visible", timeout=20000)
        await name_field.click()
        await name_field.fill("")
        await name_field.type(self.pair["name"], delay=50)
        logger.info(f"Введено имя: {self.pair['name']}")

        try:
            join_btn = page.locator(JOIN_BUTTON).first
            await join_btn.wait_for(state="visible", timeout=10000)
            await self._human_click(join_btn)
            logger.info("Нажата кнопка входа в конференцию (Join)")
        except PWTimeout:
            logger.info("Кнопка Join не найдена, отправляем Enter в поле имени")
            await page.press(NAME_INPUT, "Enter")

        await self._handle_audio_dialog()

    async def _handle_audio_dialog(self):
        """Нажимает 'Join Audio by Computer' если вылезает модалка."""
        audio_selectors = [
            "button:has-text('Join Audio by Computer')",
            "button:has-text('Войти с компьютера')",
            "button:has-text('Join Audio')",
            "button.join-audio-container__btn",
            ".join-audio-by-win"
        ]
        for _ in range(3):
            await asyncio.sleep(1.5)
            for sel in audio_selectors:
                try:
                    btn = await self._page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        logger.info("Подключили звук через компьютер (Join Audio)")
                        return
                except Exception:
                    continue

    async def _wake_footer(self):
        """Двигает мышь внизу экрана, чтобы проявить скрывающийся футер Zoom."""
        try:
            await self._page.mouse.move(300, 700)
            await asyncio.sleep(0.1)
            await self._page.mouse.move(680, 750)
            await asyncio.sleep(0.1)
        except Exception:
            pass

    async def _is_in_meeting(self) -> bool:
        for marker in IN_MEETING_MARKERS:
            try:
                el = await self._page.query_selector(marker)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _is_meeting_ended(self) -> bool:
        for marker in MEETING_ENDED_MARKERS:
            try:
                el = await self._page.query_selector(marker)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        try:
            url = self._page.url
            if "endmeeting" in url or "leave" in url.lower():
                return True
        except Exception:
            pass
        return False

    async def _is_waiting_room(self) -> bool:
        for marker in WAITING_ROOM_MARKERS:
            try:
                el = await self._page.query_selector(marker)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _debug_screenshot(self, tag: str):
        if not self._page or self._page.is_closed():
            return
        try:
            path = f"debug_{tag}.png"
            await self._page.screenshot(path=path, full_page=True)
            logger.info(f"Debug-скриншот сохранён: {path}")
        except Exception as e:
            logger.warning(f"Не удалось сохранить debug-скриншот {tag}: {e}")

    async def _mute_camera_and_mic(self):
        await self._debug_screenshot("before_mute")
        try:
            await self._wake_footer()
            await asyncio.sleep(1)

            mic_btn = await self._page.query_selector("button[aria-label*='Mute'], button[aria-label*='mute'], button[aria-label*='Выключить звук']")
            if mic_btn:
                label = (await mic_btn.get_attribute("aria-label") or "").lower()
                if "unmute" not in label and "включить" not in label:
                    await mic_btn.click()
                    logger.info("Микрофон успешно выключен")

            cam_btn = await self._page.query_selector("button[aria-label*='Stop Video'], button[aria-label*='stop video'], button[aria-label*='Остановить видео']")
            if cam_btn:
                label = (await cam_btn.get_attribute("aria-label") or "").lower()
                if "start video" not in label and "включить" not in label:
                    await cam_btn.click()
                    logger.info("Камера выключена")

        except Exception as e:
            logger.warning(f"Не удалось переключить медиафайлы: {e}")

    async def _leave(self):
        logger.info("Выполняю выход из конференции...")
        if not self._page or self._page.is_closed():
            return

        await self._wake_footer()
        await asyncio.sleep(1)
        await self._debug_screenshot("before_leave_click")

        leave_selectors = [
            "button:has-text('Leave')",
            "button:has-text('Покинуть')",
            "button:has-text('Выйти')",
            "button.footer-button__button-icon--leave",
            "div.footer__leave-btn-container button",
            "button[aria-label*='Leave']",
            "button[aria-label*='Покинуть']"
        ]

        clicked_first = False
        for sel in leave_selectors:
            try:
                el = await self._page.query_selector(sel)
                if el and await el.is_visible():
                    await self._human_click(el)
                    logger.info(f"Кликнули по первой кнопке выхода ({sel})")
                    clicked_first = True
                    break
            except Exception:
                continue

        if not clicked_first:
            logger.warning("Первая кнопка выхода не найдена обычным путем, будим футер кликом")
            await self._wake_footer()

        await asyncio.sleep(1.5)

        confirm_selectors = [
            "button.leave-meeting-options__btn",
            "div.zm-modal-body button",
            "button:has-text('Leave Meeting')",
            "button:has-text('Выйти из конференции')",
            "button:has-text('Покинуть конференцию')",
            "button:has-text('Leave')"
        ]

        for c_sel in confirm_selectors:
            try:
                c_btn = await self._page.query_selector(c_sel)
                if c_btn and await c_btn.is_visible():
                    await c_btn.click()
                    logger.info(f"Нажата кнопка подтверждения выхода в модальном окне ({c_sel})")
                    break
            except Exception:
                continue

        await asyncio.sleep(2)
        await self._debug_screenshot("after_leave_click")

    async def run(self) -> str:
        hb_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._launch()
            await self._join_flow()

            elapsed = 0
            poll_interval = 5
            in_meeting = False
            last_log = 0

            while elapsed < self.waiting_room_timeout:
                if self.stop_event.is_set():
                    logger.info("Получен stop_event во время ожидания входа")
                    await self._leave()
                    return "stopped"

                if await self._is_in_meeting():
                    in_meeting = True
                    logger.info("Успешно вошли в конференцию Zoom!")
                    break

                if elapsed - last_log >= 30:
                    waiting = await self._is_waiting_room()
                    logger.info(f"[DIAG] Статус: {elapsed}с, waiting_room={waiting}, url={self._page.url}")
                    last_log = elapsed
                    if elapsed == 30:
                        await self._debug_screenshot("waiting_room_check")

                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            if not in_meeting:
                logger.warning("Не впустили из waiting room за %s сек", self.waiting_room_timeout)
                await self._leave()
                return "failed_waiting_room"

            await self._mute_camera_and_mic()

            consecutive_absent_checks = 0

            while not self.stop_event.is_set():
                if self._page and self._page.is_closed():
                    logger.info("Страница была закрыта браузером")
                    return "in_meeting_left"

                if await self._is_meeting_ended():
                    logger.info("Встреча завершена организатором")
                    return "in_meeting_left"

                in_meeting_now = await self._is_in_meeting()

                if not in_meeting_now:
                    await self._wake_footer()
                    in_meeting_now = await self._is_in_meeting()

                if not in_meeting_now:
                    consecutive_absent_checks += 1
                    logger.warning(f"Маркер конференции не найден ({consecutive_absent_checks}/5)")
                    if consecutive_absent_checks >= 5:
                        logger.info("Маркеры пропали — скорее всего пара закончилась")
                        return "in_meeting_left"
                else:
                    consecutive_absent_checks = 0

                await asyncio.sleep(5)

            logger.info("Время пары закончилось по расписанию! Выходим из конференции...")
            await self._leave()
            return "in_meeting_left"

        except Exception as e:
            logger.exception(f"Ошибка автоматизации Zoom: {e}")
            await self._debug_screenshot("error")
            return "failed_error"

        finally:
            hb_task.cancel()
            await self._cleanup()

    async def _cleanup(self):
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception:
            pass
        finally:
            self._page = None

        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        finally:
            self._context = None

        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        finally:
            self._browser = None

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        finally:
            self._playwright = None

        logger.info("Все ресурсы Playwright очищены")