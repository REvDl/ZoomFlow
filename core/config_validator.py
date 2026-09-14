"""
Валидация config.json.

При любой ошибке -> ConfigValidationError с человекочитаемым текстом.
main.py ловит её, пишет в лог и делает sys.exit(1) ДО запуска main loop
(и до подключения к Redis/запуска браузера).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class ConfigValidationError(Exception):
    pass


VALID_DAYS = set(range(1, 8))  # 1=понедельник ... 7=воскресенье (ISO weekday)
REQUIRED_FIELDS = {"url", "name", "start", "end", "days"}


def _parse_time(value: str, field: str, pair_idx: int) -> "tuple[int, int]":
    try:
        dt = datetime.strptime(value, "%H:%M")
    except (ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"Пара #{pair_idx}: поле '{field}' = {value!r} не является временем в формате HH:MM"
        ) from exc
    return dt.hour, dt.minute


def load_and_validate(config_path: str | Path) -> list[dict[str, Any]]:
    path = Path(config_path)
    if not path.exists():
        raise ConfigValidationError(f"Файл конфигурации не найден: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigValidationError(f"Не удалось прочитать {path}: {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(f"Битый JSON в {path}: {exc}") from exc

    if not isinstance(data, list) or len(data) == 0:
        raise ConfigValidationError("config.json должен быть непустым JSON-массивом пар")

    pairs: list[dict[str, Any]] = []
    # Для проверки пересечений: (day) -> list[(start_minutes, end_minutes, idx)]
    by_day: dict[int, list[tuple[int, int, int]]] = {}

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ConfigValidationError(f"Пара #{idx}: ожидался объект, получено {type(item).__name__}")

        missing = REQUIRED_FIELDS - item.keys()
        if missing:
            raise ConfigValidationError(f"Пара #{idx}: отсутствуют обязательные поля {sorted(missing)}")

        url = item["url"]
        name = item["name"]
        days = item["days"]

        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigValidationError(f"Пара #{idx}: некорректный url {url!r}")

        if not isinstance(name, str) or not name.strip():
            raise ConfigValidationError(f"Пара #{idx}: поле 'name' пустое или не строка")

        if not isinstance(days, list) or not days:
            raise ConfigValidationError(f"Пара #{idx}: 'days' должен быть непустым списком чисел 1-7")

        for d in days:
            if not isinstance(d, int) or d not in VALID_DAYS:
                raise ConfigValidationError(
                    f"Пара #{idx}: неизвестный день недели {d!r} (допустимо 1..7, 1=понедельник)"
                )

        sh, sm = _parse_time(item["start"], "start", idx)
        eh, em = _parse_time(item["end"], "end", idx)
        start_min = sh * 60 + sm
        end_min = eh * 60 + em

        if end_min <= start_min:
            raise ConfigValidationError(
                f"Пара #{idx}: 'end' ({item['end']}) должен быть позже 'start' ({item['start']})"
            )

        for d in days:
            for (o_start, o_end, o_idx) in by_day.get(d, []):
                if start_min < o_end and o_start < end_min:
                    raise ConfigValidationError(
                        f"Пересечение по времени в день {d}: пара #{idx} "
                        f"({item['start']}-{item['end']}) пересекается с парой #{o_idx}"
                    )
            by_day.setdefault(d, []).append((start_min, end_min, idx))

        pairs.append(
            {
                "index": idx,
                "url": url,
                "name": name,
                "start": item["start"],
                "end": item["end"],
                "days": days,
            }
        )

    return pairs


if __name__ == "__main__":
    import sys

    try:
        result = load_and_validate(sys.argv[1] if len(sys.argv) > 1 else "config.json")
        print(f"OK: {len(result)} пар(ы) валидны")
    except ConfigValidationError as e:
        print(f"ОШИБКА ВАЛИДАЦИИ: {e}", file=sys.stderr)
        sys.exit(1)
