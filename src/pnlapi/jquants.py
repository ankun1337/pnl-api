"""J-Quants V2 适配（提示词第 5 节）。

端点路径与字段名全部来自官方文档 + fixture 实测（docs/fixture-notes.md），
禁止凭训练记忆书写。

纪律：
- `x-api-key` 请求头；
- 429 → 指数退避 + full jitter（基数 1s，上限 30s，最多 5 次）；5xx ≤ 2 次；
  其余 4xx 不重试直接报错；
- 串行请求，取数前对代码去重（决策 4：去重仅限取数层）；
- 缓存 code → 已解析行情，JST 日期翻转即失效（决策：严格按提示词原文）。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date, timedelta
from decimal import Decimal

import httpx

from pnlapi.cache import DayCache
from pnlapi.calendar import CalendarService
from pnlapi.clock import today_jst
from pnlapi.config import get_settings
from pnlapi.pnl import Bar

logger = logging.getLogger("pnlapi.jquants")

BASE_URL = "https://api.jquants.com"
PATH_BARS = "/v2/equities/bars/daily"
PATH_MASTER = "/v2/equities/master"

# Light 档官方限流 60 请求/分（出处 https://jpx-jquants.com/ja/spec/rate-limits）。
# 取 80% → 0.8 rps → 最小间隔 1.25s。不设为配置键（decisions.md 小决策 3）。
MIN_INTERVAL_S = 1.25

MAX_RETRY_429 = 5
MAX_RETRY_5XX = 2
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
MAX_PAGES = 50  # 翻页保险丝；单只股票 45 天区间实测 1 页


class JQuantsError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"J-Quants HTTP {status}: {message}")


def _to_decimal(value) -> Decimal | None:
    """JSON number → Decimal，经 str 中转避免二进制浮点污染金额。"""
    if value is None:
        return None
    return Decimal(str(value))


def parse_bar(row: dict) -> Bar:
    """源行 → Bar 投影。字段名来自实测（fixture-notes 第 1 节）。"""
    return Bar(
        trade_date=date.fromisoformat(row["Date"]),
        code=row["Code"],
        close=_to_decimal(row.get("C")),
        adj_close=_to_decimal(row.get("AdjC")),
        volume=_to_decimal(row.get("Vo")),
    )


class JQuantsClient:
    """串行、限速、可注入 transport（测试用 MockTransport）。"""

    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 sleeper=time.sleep) -> None:
        settings = get_settings()
        self._key = settings.jquants_api_key.get_secret_value().strip()
        self._timeout = settings.timeout_s
        self._lookback_days = settings.lookback_days
        self._sleep = sleeper
        self._last_request_at = 0.0
        self._client = httpx.Client(
            base_url=BASE_URL, timeout=self._timeout, transport=transport
        )
        # 行情缓存两档失效：当日数据钉到日切，旧数据与无数据走 TTL（检查点 2 批复）
        self._bars_cache = DayCache(stale_ttl_s=settings.cache_stale_ttl_s)
        # 主数据与日历无 as_of 语义，取到即钉到日切（put(pin=True)）
        self._master_cache = DayCache()
        self.calendar = CalendarService(self._get_paged)

    def close(self) -> None:
        self._client.close()

    # -- 传输层 ----------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_INTERVAL_S:
            self._sleep(MIN_INTERVAL_S - elapsed)
        self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict[str, str]) -> dict:
        attempt_429 = attempt_5xx = 0
        while True:
            self._throttle()
            response = self._client.get(
                path, params=params, headers={"x-api-key": self._key}
            )
            if response.status_code == 429:
                attempt_429 += 1
                if attempt_429 > MAX_RETRY_429:
                    raise JQuantsError(429, "限流重试 5 次仍失败")
                delay = random.uniform(
                    0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt_429 - 1)))
                )
                logger.warning("429 on %s，第 %d 次退避 %.1fs", path, attempt_429, delay)
                self._sleep(delay)
                continue
            if response.status_code >= 500:
                attempt_5xx += 1
                if attempt_5xx > MAX_RETRY_5XX:
                    raise JQuantsError(response.status_code, response.text[:200])
                delay = random.uniform(
                    0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt_5xx))
                )
                logger.warning("%d on %s，退避 %.1fs", response.status_code, path, delay)
                self._sleep(delay)
                continue
            if response.status_code != 200:
                # 4xx（非 429）不重试（提示词第 5 节）
                raise JQuantsError(response.status_code, response.text[:200])
            return response.json()

    def _get_paged(self, path: str, params: dict[str, str]) -> list[dict]:
        """翻页循环至翻页键消失（字段名 pagination_key，实测确认）。"""
        rows: list[dict] = []
        page_params = dict(params)
        for _ in range(MAX_PAGES):
            body = self._get(path, page_params)
            rows.extend(body.get("data", []))
            key = body.get("pagination_key")
            if not key:
                return rows
            page_params = dict(params) | {"pagination_key": key}
        raise JQuantsError(0, f"翻页超过保险丝 {MAX_PAGES} 页")

    # -- 主数据：名称 + 代码有效性（决策 3、4）---------------------------

    def _load_master(self) -> tuple[dict[str, str], set[str]]:
        """返回 (code → name_ja, 全部 code 集合)。当日缓存。

        失败时返回 (空表, 空集)——名称置 null 不阻塞计算（提示词第 5 节），
        代码有效性判定退化由调用方处理（决策 3）。
        """
        cached = self._master_cache.get("master")
        if cached is not None:
            return cached
        try:
            rows = self._get_paged(PATH_MASTER, {})
        except Exception as exc:  # noqa: BLE001 - 主数据失败绝不阻塞盈亏
            logger.warning("主数据拉取失败，名称将为 null 且代码有效性未验证: %s", exc)
            return {}, set()
        name_map = {r["Code"]: r.get("CoName") for r in rows}
        result = (name_map, set(name_map))
        # 主数据无 as_of 语义，且已确实取到数据 → 显式钉到日切
        self._master_cache.put("master", result, pin=True)
        logger.info("主数据已加载：%d 只证券", len(name_map))
        return result

    def resolve_code(self, user_code: str) -> tuple[str | None, bool]:
        """调用方代码 → 供应商 5 位代码（决策 4）。

        返回 (供应商代码 | None, 主数据是否可用)。
        以主数据双向表为准；主数据不可用时用「四位 + 补 0」降级规则。
        None 表示在主数据中确认不存在 → UNKNOWN_CODE。
        """
        code = user_code.strip().upper()
        name_map, code_set = self._load_master()
        master_ok = bool(code_set)

        if not master_ok:
            # 降级：四位补 0，无法验证有效性
            return (code + "0" if len(code) == 4 else code), False
        if code in code_set:
            return code, True
        padded = code + "0"
        if len(code) == 4 and padded in code_set:
            return padded, True
        return None, True

    def name_of(self, vendor_code: str) -> str | None:
        name_map, _ = self._load_master()
        return name_map.get(vendor_code)

    # -- 行情 ------------------------------------------------------------

    def fetch_bars(self, vendor_code: str) -> list[Bar]:
        """取该代码回看窗口内的日线，按日期升序。当日缓存。"""
        cached = self._bars_cache.get(vendor_code)
        if cached is not None:
            return cached
        today = today_jst()
        start = today - timedelta(days=self._lookback_days)
        rows = self._get_paged(
            PATH_BARS,
            {"code": vendor_code, "from": start.isoformat(), "to": today.isoformat()},
        )
        bars = sorted(
            (parse_bar(r) for r in rows), key=lambda b: b.trade_date
        )
        # as_of = 该批数据的最新交易日，决定缓存走哪一档：
        # 已是当日数据 → 钉到日切；仍是旧数据 → TTL 后重取，让就绪协议可用
        as_of = bars[-1].trade_date if bars else None
        self._bars_cache.put(vendor_code, bars, as_of=as_of)
        return bars

    def reachable(self) -> bool:
        """/v1/health 用：一次轻量探测，不抛异常。"""
        try:
            self._get(PATH_BARS, {"code": "7203", "date": today_jst().isoformat()})
            return True
        except Exception:  # noqa: BLE001
            return False
