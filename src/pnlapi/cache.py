"""进程内缓存：code -> 已解析行情。

失效规则：**JST 日期翻转即失效**（提示词第 5 节原文；2026-08-23 Jack 拍板
严格按原文执行，见 docs/decisions.md）。

已向 Jack 报告并由其接受的已知行为：若某代码在当日收盘价发布（约 16:30 JST）
**之前**被请求过，缓存会将旧 as_of 钉到当日 JST 午夜——当天 16:30 后也拿不到
新价。规避方法写入 consumer-api.md：当日首次请求放在 16:30 JST 之后。
"""

from __future__ import annotations

import threading
from datetime import date
from typing import Any

from pnlapi.clock import today_jst


class DayCache:
    """线程安全。条目仅在写入当天（JST）有效，日期翻转后视为不存在。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[date, Any]] = {}

    def get(self, key: str) -> Any | None:
        today = today_jst()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stored_day, value = entry
            if stored_day != today:
                del self._data[key]
                return None
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (today_jst(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        today = today_jst()
        with self._lock:
            return sum(1 for day, _ in self._data.values() if day == today)
