"""进程内缓存：code -> 已解析行情。

失效规则（2026-08-23 检查点 2 批复修正，根因是提示词第 5 节的规格缺陷）：

- 条目的 **as_of == 今天(JST)** → 缓存至 JST 日切。EOD 数据当天不再变，
  钉住是正确的，也挡掉了当日的重复轮询。
- 条目的 **as_of < 今天(JST)** → 只缓存 `CACHE_STALE_TTL_S` 秒（默认 600）。

**为什么必须区分**：原规格「日期翻转即失效」会让当日 16:30 JST 之前的一次
请求把旧 as_of 钉到午夜——当天收盘价发布后也取不到新价，消费方的
「等待-重试」就绪协议永久失效。给旧数据加 TTL 后，发布前的轮询仍被挡掉
绝大部分，发布后 10 分钟内自然取到新价，协议恢复可用。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from pnlapi.clock import today_jst


@dataclass(frozen=True)
class _Entry:
    value: Any
    stored_day: date      # 写入时的 JST 日期
    as_of: date | None    # 该条目所承载数据的业务日期
    pinned: bool          # True=钉到日切；False=走 TTL
    stored_at: float      # 单调时钟，用于 TTL


class DayCache:
    """线程安全。失效策略：

    - `as_of == 今天` → 钉到 JST 日切（EOD 当天不再变）
    - `as_of < 今天` → TTL（收盘价发布后能自然取到新价）
    - `as_of is None` → **TTL**。这表示"没取到任何可用数据"，
      是**可恢复状态**，不得钉住（检查点 2 后续批复第 2 条）
    - `pin=True` 显式钉住 → 用于主数据这类无 as_of 语义、且确实取到了数据的条目
    """

    def __init__(self, stale_ttl_s: float = 600.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, _Entry] = {}
        self._stale_ttl_s = stale_ttl_s
        self._monotonic = monotonic

    def _is_live(self, entry: _Entry, today: date, now: float) -> bool:
        if entry.stored_day != today:
            return False                      # 跨日一律失效
        if entry.pinned:
            return True                       # 显式钉住（主数据）
        if entry.as_of is not None and entry.as_of == today:
            return True                       # 当日数据：钉到日切
        # 旧数据，或 as_of=None（无可用数据，可恢复状态）：走 TTL
        return now - entry.stored_at < self._stale_ttl_s

    def get(self, key: str) -> Any | None:
        today, now = today_jst(), self._monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if not self._is_live(entry, today, now):
                del self._data[key]
                return None
            return entry.value

    def put(self, key: str, value: Any, *, as_of: date | None = None,
            pin: bool = False) -> None:
        """写入条目。

        `as_of`：该条目数据的业务日期。为 None 表示无可用数据 → 走 TTL。
        `pin`：显式钉到日切，供主数据这类无 as_of 语义的条目使用。
        """
        with self._lock:
            self._data[key] = _Entry(
                value=value,
                stored_day=today_jst(),
                as_of=as_of,
                pinned=pin,
                stored_at=self._monotonic(),
            )

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        today, now = today_jst(), self._monotonic()
        with self._lock:
            return sum(1 for e in self._data.values() if self._is_live(e, today, now))
