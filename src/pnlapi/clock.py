"""JST 时间工具。全仓禁止裸调 datetime.now()（提示词第 3 节）。

JST = UTC+9，无夏令时。用固定偏移而非 zoneinfo，不依赖系统 tz 数据库。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9), name="JST")


def now_jst() -> datetime:
    return datetime.now(JST)


def today_jst() -> date:
    return now_jst().date()
