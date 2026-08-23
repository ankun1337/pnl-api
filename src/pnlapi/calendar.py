"""交易日历（检查点 2 后续批复第 3 条批准接入）。

用途：让服务自己区分「今天非交易日」与「当日收盘价尚未发布」，
把消费方的等待判断从猜测变成确定。

纪律：
- 进程内缓存，JST 日切失效，首次请求惰性拉取；
- 映射 HolDiv "1" → 交易日；"0" 与 "3" → 非交易日；
  **未知取值 → is_trading_day = null + WARN，禁止猜**；
- 拉取失败绝不阻塞盈亏计算，一律降级为 null（与名称同源则）。

`HolDiv` 取值含义（实测 + 官方 /ja/spec/mkt-cal/holiday-division）：
  0 = 非営業日
  1 = 営業日
  2 = 東証半日立会日
  3 = 非営業日（OSE 祝日取引あり）——东证现货当天不交易
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from pnlapi.cache import DayCache
from pnlapi.clock import today_jst

logger = logging.getLogger("pnlapi.calendar")

PATH_CALENDAR = "/v2/markets/calendar"

# HolDiv → is_trading_day。仅这三个取值被明确映射；其余一律 None + WARN。
# "2"（东证半日立会日）实测未在窗口内出现，但语义明确属于交易日，一并映射。
_TRADING_BY_HOLDIV: dict[str, bool] = {
    "0": False,   # 非营业日
    "1": True,    # 营业日
    "2": True,    # 东证半日立会日：仍然开市
    "3": False,   # 非营业日（仅 OSE 祝日衍生品交易，东证现货不开）
}

# 拉取窗口：往前 30 天覆盖回看需求，往后 45 天覆盖未来查询
_LOOKBEHIND_DAYS = 30
_LOOKAHEAD_DAYS = 45

_CACHE_KEY = "calendar"


@dataclass(frozen=True)
class CalendarDay:
    calendar_date: date
    holiday_division: str       # HolDiv 原值，透传
    is_trading_day: bool | None # 派生值；未知 HolDiv 时为 None


def map_holdiv(raw: str) -> bool | None:
    """HolDiv → is_trading_day。未知取值返回 None 并 WARN（禁止猜）。"""
    if raw in _TRADING_BY_HOLDIV:
        return _TRADING_BY_HOLDIV[raw]
    logger.warning(
        "交易日历出现未知 HolDiv 取值 %r，is_trading_day 置 null（不猜测）", raw
    )
    return None


class CalendarService:
    """惰性拉取 + 日切失效。任何失败都降级为「日历不可用」，不抛异常。"""

    def __init__(self, fetch_pages) -> None:
        """fetch_pages: 与 JQuantsClient._get_paged 同签名的可调用对象。"""
        self._fetch_pages = fetch_pages
        self._cache = DayCache()

    def _load(self) -> dict[date, CalendarDay] | None:
        """返回 {date: CalendarDay}；不可用时返回 None。"""
        cached = self._cache.get(_CACHE_KEY)
        if cached is not None:
            return cached

        today = today_jst()
        params = {
            "from": (today - timedelta(days=_LOOKBEHIND_DAYS)).isoformat(),
            "to": (today + timedelta(days=_LOOKAHEAD_DAYS)).isoformat(),
        }
        try:
            rows = self._fetch_pages(PATH_CALENDAR, params)
        except Exception as exc:  # noqa: BLE001 - 日历失败绝不阻塞盈亏
            logger.warning("交易日历拉取失败，相关字段将为 null: %s", exc)
            return None

        days: dict[date, CalendarDay] = {}
        for row in rows:
            try:
                day = date.fromisoformat(row["Date"])
            except (KeyError, ValueError):
                logger.warning("交易日历行日期不可解析，已跳过: %r", row)
                continue
            raw = str(row.get("HolDiv", ""))
            days[day] = CalendarDay(
                calendar_date=day,
                holiday_division=raw,
                is_trading_day=map_holdiv(raw),
            )

        if not days:
            logger.warning("交易日历返回空，相关字段将为 null")
            return None

        # 日历本身当日不变，钉到 JST 日切
        self._cache.put(_CACHE_KEY, days, pin=True)
        logger.info(
            "交易日历已加载：%d 天（%s … %s）",
            len(days), min(days).isoformat(), max(days).isoformat(),
        )
        return days

    def is_trading_day(self, day: date) -> bool | None:
        """None 表示：日历不可用、该日期超出覆盖范围、或 HolDiv 取值未知。"""
        days = self._load()
        if days is None:
            return None
        entry = days.get(day)
        return entry.is_trading_day if entry else None

    def is_trading_day_today(self) -> bool | None:
        return self.is_trading_day(today_jst())

    def range(self, start: date, end: date) -> list[dict] | None:
        """[start, end] 逐日返回。超出源覆盖范围的日期显式标 not_covered。

        日历整体不可用时返回 None，由调用方决定如何呈现。
        """
        days = self._load()
        if days is None:
            return None

        covered_min, covered_max = min(days), max(days)
        result: list[dict] = []
        cursor = start
        while cursor <= end:
            entry = days.get(cursor)
            if entry is not None:
                result.append({
                    "date": cursor,
                    "holiday_division": entry.holiday_division,
                    "is_trading_day": entry.is_trading_day,
                    "not_covered": False,
                })
            else:
                # 源没有这一天：可能超出范围，也可能源本身缺行。不猜。
                result.append({
                    "date": cursor,
                    "holiday_division": None,
                    "is_trading_day": None,
                    "not_covered": True,
                })
            cursor += timedelta(days=1)
        return result

    def coverage(self) -> tuple[date, date] | None:
        days = self._load()
        if not days:
            return None
        return min(days), max(days)
