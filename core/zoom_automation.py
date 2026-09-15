"""
Автоматизация входа/выхода из Zoom-конференции через Zoom Web Client
(без установки десктопного приложения — избегаем deep-link на "Open Zoom App").

ВАЖНО: Zoom регулярно меняет вёрстку веб-клиента. Селекторы ниже —
рабочая отправная точка, а не гарантия "навечно". Если Zoom изменит
DOM, потребуется поправить CSS-селекторы в этом файле.
"""
from __future__ import annotations
import re
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

MEETING_ENDED_MARKERS = [
    "text=This meeting has been ended by host",
    "text=Meeting has ended",
    "text=Встреча завершена",
    "text=конференция завершена организатором",
    "text=has ended",
    "text=Meeting ended by host",
]

JOIN_FROM_BROWSER_LINK = "a#wc_bottom_btn, a[href*='/wc/join/'], a:has-text('Join from Your Browser'), a:has-text('Присоединиться из браузера'), .js-fallback"
NAME_INPUT = "#inputname, input[name='name'], input[placeholder*='Name'], input[placeholder*='имя'], #input-for-name"
JOIN_BUTTON = "#joinBtn, button:has-text('Join'), button:has-text('Войти'), button.preview-join-button"

IN_MEETING_MARKER = "#wc-container-right, div.footer__leave-btn-container, div.footer-button__button-handle"

FOOTER_HANDLE = "div.footer-button__button-handle"
LEAVE_BUTTON = "button:has-text('Leave'), button:has-text('Покинуть')"
LEAVE_CONFIRM_BUTTON = (
    "button:has-text('Выйти из конференции'), "
    "button:has-text('Покинуть конференцию'), "
    "button:has-text('Leave Meeting'), "
    "button:has-text('Leave meeting'), "
    "button:has-text('End Meeting')"
)


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
            "--disable-software-rasterizer",
            "--use-fake-ui-for-media-stream",  # авто-разрешение mic/cam промптов (в связке с context permissions)
            "--use-fake-device-for-media-stream",  # фейковое устройство, чтобы Zoom не завис ожидая реальное железо
            "--disable-blink-features=AutomationControlled",
            "--no-zygote",
            "--renderer-process-limit=1",
            "--js-flags=--max-old-space-size=128",
            "--use-gl=swiftshader",
            "--disable-accelerated-2d-canvas",
            "--disable-canvas-aa",
        ]

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=LOW_RAM_ARGS,
        )

        self._context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 800},
            permissions=["microphone"],
        )
        self._context.set_default_timeout(15000)
        self._context.set_default_navigation_timeout(20000)

        self._page = await self._context.new_page()
        self._page.on("dialog", lambda dialog: asyncio.create_task(self._handle_dialog(dialog)))
        self._page.on("console", lambda msg: logger.info(f"[console:{msg.type}] {msg.text}"))
        self._page.on("pageerror", lambda err: logger.warning(f"[pageerror] {err}"))
        async def _intercept_route(route):
            if route.request.resource_type == "image":
                await route.abort()
            else:
                await route.continue_()

        await self._page.route("**/*", _intercept_route)

        await self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

    async def _handle_dialog(self, dialog):
        logger.info(f"[DIAG] Нативный диалог: type={dialog.type!r} message={dialog.message!r}")
        await dialog.accept()

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
        try:
            cf_frame = page.frame_locator("iframe[src*='challenges.cloudflare.com']")
            cf_checkbox = cf_frame.locator("input[type='checkbox'], #challenge-stage")
            if await cf_checkbox.is_visible(timeout=3000):
                logger.info("Обнаружен чекбокс Cloudflare, пробуем нажать...")
                await cf_checkbox.click()
                await page.wait_for_timeout(3000)
        except Exception:
            pass
        name_field = await page.wait_for_selector(NAME_INPUT, state="visible", timeout=20000)
        await name_field.fill(self.pair["name"])
        try:
            join_btn = page.locator(JOIN_BUTTON)
            await join_btn.wait_for(state="visible", timeout=10000)
            await page.wait_for_function(
                """(sel) => {
                    const el = document.querySelector(sel.split(',')[0].trim());
                    return el && !el.disabled;
                }""",
                arg=JOIN_BUTTON,
                timeout=10000,
            )
            await name_field.fill(self.pair["name"])
            await join_btn.click()
            logger.info("Нажата кнопка входа в конференцию")
        except PWTimeout:
            logger.info("Кнопка Join не найдена, отправляем Enter в инпут")
            await page.press(NAME_INPUT, "Enter")

    async def _wake_footer(self):
        """
        Footer-панель (Leave/Mute/Video) в Zoom Web Client часто скрыта
        по умолчанию и появляется только по движению мыши внутри окна
        встречи. Без этого div.footer-button__button-handle никогда не
        станет visible, и клики по Leave/Mute будут тайм-аутиться вслепую.
        """
        try:
            # Двигаем мышь в нижнюю треть экрана, где обычно живёт footer
            await self._page.mouse.move(200, 700)
            await asyncio.sleep(0.3)
            await self._page.mouse.move(500, 730)
            await asyncio.sleep(0.3)
            await self._page.mouse.move(512, 760)
        except Exception:
            pass

    async def _is_in_meeting(self) -> bool:
        try:
            await self._page.wait_for_selector(IN_MEETING_MARKER, timeout=2000)
            return True
        except PWTimeout:
            return False

    async def _is_meeting_ended(self) -> bool:
        for marker in MEETING_ENDED_MARKERS:
            try:
                el = await self._page.query_selector(marker)
                if el:
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
                if el:
                    return True
            except Exception:
                continue
        return False

    async def _debug_screenshot(self, tag: str):
        """Сохраняет скриншот текущего состояния страницы для отладки селекторов."""
        if not self._page or self._page.is_closed():
            return
        try:
            path = f"debug_{tag}.png"
            await self._page.screenshot(path=path, full_page=True)
            logger.info(f"Debug-скриншот сохранён: {path}")
        except Exception as e:
            logger.warning(f"Не удалось сохранить debug-скриншот {tag}: {e}")

    async def _mute_camera_and_mic(self):
        """Безопасное отключение микрофона и камеры только при необходимости."""
        await self._debug_screenshot("before_mute")
        try:
            await self._wake_footer()
            await self._page.wait_for_selector(LEAVE_BUTTON, timeout=8000)
            await asyncio.sleep(2)  # Даем гидратации React завершиться

            # 1. Микрофон: проверяем aria-label кнопки
            mic_btn = await self._page.query_selector("button[aria-label*='Mute'], button[aria-label*='mute']")
            if mic_btn:
                label = (await mic_btn.get_attribute("aria-label") or "").lower()
                if "unmute" not in label and "mute" in label:
                    await mic_btn.click()
                    logger.info("Микрофон успешно выключен")

            # 2. Камера: разрешения на неё нет (см. _launch), поэтому она
            # физически не сможет включиться — никакого клика не требуется.
            # Оставляю только защитную проверку на случай, если Zoom всё же
            # предложит fallback-камеру — тогда молча выключим.
            cam_btn = await self._page.query_selector(
                "button[aria-label*='Stop Video'], button[aria-label*='stop video']")
            if cam_btn:
                label = (await cam_btn.get_attribute("aria-label") or "").lower()
                if "start video" not in label and "stop" in label:
                    await cam_btn.click()
                    logger.info("Камера была неожиданно включена — выключил")

        except Exception as e:
            logger.warning(f"Не удалось переключить медиафайлы (возможно, уже выключены или хост заблокировал): {e}")
            await self._debug_screenshot("mute_failed")

    async def _click_leave_humanlike(self) -> bool:
        box = await self._page.locator(LEAVE_BUTTON).bounding_box()
        if not box:
            return False
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await self._page.mouse.move(cx - 5, cy - 5)
        await asyncio.sleep(0.1)
        await self._page.mouse.move(cx, cy, steps=5)
        await asyncio.sleep(0.15)
        await self._page.mouse.down()
        await asyncio.sleep(0.12)
        await self._page.mouse.up()
        await asyncio.sleep(0.3)
        return True

    async def _leave(self):
        logger.info("Выполняю выход из конференции...")
        if not self._page or self._page.is_closed():
            return

        await self._wake_footer()
        try:
            await self._page.wait_for_selector(LEAVE_BUTTON, state="visible", timeout=5000)
            await asyncio.sleep(0.5)
        except Exception:
            logger.warning("Footer-панель не появилась перед выходом")

        await self._debug_screenshot("before_leave_click")

        clicked = False
        try:
            clicked = await self._click_leave_humanlike()
            logger.info(f"Клик по Leave (humanlike) выполнен: {clicked}")
        except Exception as e:
            logger.warning(f"Humanlike-клик по Leave не сработал: {e}")

        await asyncio.sleep(1.5)
        await self._debug_screenshot("after_leave_click")

        if await self._is_meeting_ended():
            logger.info("Встреча завершена, выход подтверждён")
            return

        logger.warning("Confirm не подтверждён после humanlike-клика — закрываю страницу как fallback")
        try:
            await self._page.close()
        except Exception:
            pass


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
                    return "stopped"
                if await self._is_in_meeting():
                    in_meeting = True
                    break
                if elapsed - last_log >= 30:
                    waiting = await self._is_waiting_room()
                    logger.info(f"[DIAG] Ожидание входа: {elapsed}с, waiting_room={waiting}, url={self._page.url}")
                    last_log = elapsed
                    if elapsed == 30:  # один раз, для диагностики
                        await self._debug_screenshot("waiting_room_check")
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            if not in_meeting:
                logger.warning("Не впустили из waiting room за %s сек", self.waiting_room_timeout)
                return "failed_waiting_room"

            await self._mute_camera_and_mic()

            # Основное ожидание до конца пары / команды stop
            consecutive_absent_checks = 0

            while not self.stop_event.is_set():
                if self._page and self._page.is_closed():
                    logger.info("Страница была закрыта")
                    return "in_meeting_left"

                if await self._is_meeting_ended():
                    logger.info("Обнаружен явный маркер завершения встречи")
                    return "in_meeting_left"

                in_meeting_now = await self._is_in_meeting()

                if not in_meeting_now:
                    consecutive_absent_checks += 1
                    logger.warning(f"Маркер конференции не найден ({consecutive_absent_checks}/3)")
                    if consecutive_absent_checks >= 3:
                        logger.info("Конференция завершена хостом или произошел вылет")
                        return "in_meeting_left"
                else:
                    consecutive_absent_checks = 0

                await asyncio.sleep(5)

            await self._leave()
            return "in_meeting_left"

        except Exception as e:
            logger.exception(f"Ошибка автоматизации Zoom: {e}")
            if self._page and not self._page.is_closed():
                try:
                    await self._page.screenshot(path="debug_error.png", full_page=True)
                    logger.info("Скриншот ошибки сохранен в корень контейнера как debug_error.png")
                except Exception as scr_err:
                    logger.error(f"Не удалось сделать скриншот: {scr_err}")
            return "failed_error"

        finally:
            hb_task.cancel()
            await self._cleanup()

    async def _cleanup(self):
        """Безопасная очистка ресурсов без падения Event Loop."""
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