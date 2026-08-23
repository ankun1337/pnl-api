"""取价规则与盈亏数学（纯函数，禁止偏离提示词第 7 节）。

本模块不做任何 I/O，不认识 httpx，只接受已解析的行记录。
这是"算对钱高于一切"的落点——所有数学在此，路由层禁止重写。

两条不可混用的口径（提示词第 7 节 + 红线）：
- **估值用原始价** `C`：市场真实成交价，乘以当前股数才是真实市值；
- **当日涨跌用复权价** `AdjC` 相除：跨除权日才不会算出假涨跌。

金额一律用 Decimal，不用 float——这是要公开发布的盈亏数字。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

# --- 错误码（对外契约，consumer-api.md 的错误目录与此一一对应）------------

ERR_UNKNOWN_CODE = "UNKNOWN_CODE"
ERR_NO_RECENT_PRICE = "NO_RECENT_PRICE"
ERR_INVALID_SHARES = "INVALID_SHARES"
ERR_INVALID_COST = "INVALID_COST"

HINT_MASTER_UNAVAILABLE = "主数据不可用，代码有效性未验证"


class RowStatus(str, Enum):
    """单行日线的三态判定（决策 1）。"""

    NORMAL = "NORMAL"
    NO_TRADE = "NO_TRADE"
    ANOMALOUS = "ANOMALOUS"  # 部分 null：既非全空也非关键字段齐备，跳过并 WARN


@dataclass(frozen=True)
class Bar:
    """一行日线的最小投影。原始 JSON 由 jquants.py 转成本结构。"""

    trade_date: date
    code: str
    close: Decimal | None       # 源 C，原始价
    adj_close: Decimal | None   # 源 AdjC，复权价
    volume: Decimal | None      # 源 Vo


@dataclass(frozen=True)
class PriceInfo:
    """取价结果。"""

    latest_close: Decimal       # 估值用原始价
    as_of: date
    pct_change_today: Decimal | None


@dataclass(frozen=True)
class PnlResult:
    cost_total: Decimal
    market_value: Decimal
    profit: Decimal
    profit_rate: Decimal


def classify(bar: Bar) -> RowStatus:
    """三态判定（决策 1）。

    实测事实（docs/fixture-notes.md 第 2 节）：无成交行的 13 个字段**同时**为
    null，5 个样本结构完全一致，不存在中间形态。故：

    - C、AdjC、Vo **全为 null** → NO_TRADE，正常跳过；
    - C、AdjC、Vo **全非 null** → NORMAL，可进入计算；
    - 其余任何组合（部分 null）→ ANOMALOUS，跳过并由调用方 WARN。

    Jack 在检查点 1 追加：latest 行必须 C 与 AdjC 均非 null 方可进入计算。
    这里进一步要求 Vo 也非 null——有价无量同样是残缺数据，实测中不该出现；
    真出现了说明上游形态变了，宁可跳过一行，不可用残缺数据算钱。
    """
    fields = (bar.close, bar.adj_close, bar.volume)
    if all(f is None for f in fields):
        return RowStatus.NO_TRADE
    if all(f is not None for f in fields):
        return RowStatus.NORMAL
    return RowStatus.ANOMALOUS


def pick_price(bars: list[Bar]) -> tuple[PriceInfo | None, list[Bar]]:
    """按提示词第 7 节取价。

    输入按日期升序（调用方保证）。返回 (取价结果, 异常行列表)。
    无可用 NORMAL 行时返回 (None, 异常行)——调用方据此产出 NO_RECENT_PRICE。

        latest       = 最近一个 NORMAL 行
        latest_close = latest.close            (原始价)
        as_of        = latest.trade_date
        prev         = latest 之前最近一个 NORMAL 行
        pct_change   = latest.adj_close / prev.adj_close - 1   (复权价)
        prev 不存在  → pct_change = None
    """
    anomalies = [b for b in bars if classify(b) is RowStatus.ANOMALOUS]
    normals = [b for b in bars if classify(b) is RowStatus.NORMAL]
    if not normals:
        return None, anomalies

    latest = normals[-1]
    prev = normals[-2] if len(normals) >= 2 else None

    pct: Decimal | None = None
    if prev is not None and prev.adj_close and prev.adj_close != 0:
        # 用复权列相除：跨除权日不会算出假涨跌
        pct = latest.adj_close / prev.adj_close - Decimal(1)  # type: ignore[operator]

    return (
        PriceInfo(
            latest_close=latest.close,  # type: ignore[arg-type]  classify 已保证非 None
            as_of=latest.trade_date,
            pct_change_today=pct,
        ),
        anomalies,
    )


# --- 输入校验（持仓级，返回错误码而非抛异常）-------------------------------


def validate_shares(shares: Any) -> str | None:
    """shares 必须是正整数。返回错误码或 None。"""
    if isinstance(shares, bool) or not isinstance(shares, int):
        return ERR_INVALID_SHARES
    if shares <= 0:
        return ERR_INVALID_SHARES
    return None


def resolve_cost_total(
    shares: int, cost_price: Decimal | None, cost_total: Decimal | None
) -> tuple[Decimal | None, str | None]:
    """成本二选一（提示词第 7 节）。

    双给 / 双缺由请求体 schema 层拦截（结构性错误 → 整体 400，见 decisions.md
    「错误层级读法」）。本函数是纵深防御，同时负责数值合法性（持仓级错误）。
    """
    if (cost_price is None) == (cost_total is None):
        return None, ERR_INVALID_COST
    try:
        total = cost_total if cost_total is not None else cost_price * shares  # type: ignore[operator]
    except (InvalidOperation, TypeError):
        return None, ERR_INVALID_COST
    if total is None or total <= 0:
        return None, ERR_INVALID_COST
    return total, None


def compute_pnl(
    shares: int, cost_total: Decimal, latest_close: Decimal
) -> PnlResult:
    """毛盈亏，不含手续费与税金（meta 固定 fees_included=false）。

        market_value = latest_close × shares     -- 原始价估值
        profit       = market_value − cost_total
        profit_rate  = profit / cost_total
    """
    market_value = latest_close * shares
    profit = market_value - cost_total
    return PnlResult(
        cost_total=cost_total,
        market_value=market_value,
        profit=profit,
        profit_rate=profit / cost_total,  # cost_total > 0 由 resolve_cost_total 保证
    )


def aggregate(results: list[PnlResult], had_errors: bool) -> dict[str, Any]:
    """totals 只汇总成功项；存在失败项时 partial=true（提示词第 7 节）。"""
    cost = sum((r.cost_total for r in results), Decimal(0))
    value = sum((r.market_value for r in results), Decimal(0))
    profit = value - cost
    return {
        "cost_total": cost,
        "market_value": value,
        "profit": profit,
        "profit_rate": (profit / cost) if cost > 0 else None,
        "partial": had_errors,
    }
