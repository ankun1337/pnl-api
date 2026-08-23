"""T1–T3、T8：解析、取价回退、跨拆股复权、异常行防御。

全部基于真实 fixture（tests/fixtures/），禁止手写假响应。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pnlapi.jquants import parse_bar
from pnlapi.pnl import Bar, RowStatus, classify, pick_price

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


# --- T1 解析：fixture → 模型 → 逐字段对照 ---------------------------------


def test_t1_parse_normal_rows():
    rows = load("bars_normal_7203")
    assert rows, "fixture 应非空"
    for raw in rows:
        bar = parse_bar(raw)
        assert bar.trade_date == date.fromisoformat(raw["Date"])
        assert bar.code == raw["Code"]
        assert bar.close == Decimal(str(raw["C"]))
        assert bar.adj_close == Decimal(str(raw["AdjC"]))
        assert bar.volume == Decimal(str(raw["Vo"]))
        # Decimal 经 str 中转，无二进制浮点尾巴
        assert str(bar.close) == str(raw["C"])


def test_t1_parse_preserves_code_as_string():
    """证券代码始终按字符串处理（红线：禁止转数值、禁止截断）。"""
    rows = load("bars_no_trade_sample")
    codes = [parse_bar(r).code for r in rows]
    assert all(isinstance(c, str) for c in codes)
    # 实测样本含字母代码
    assert any(not c.isdigit() for c in codes), "fixture 应含字母代码样本"


def test_t1_parse_no_trade_rows_yield_none():
    rows = [r for r in load("bars_no_trade_sample") if r["O"] is None]
    assert rows, "fixture 应含无成交行"
    for raw in rows:
        bar = parse_bar(raw)
        assert bar.close is None and bar.adj_close is None and bar.volume is None
        assert bar.trade_date and bar.code  # 日期与代码仍有值


# --- 三态判定（决策 1）-----------------------------------------------------


def test_classify_from_real_fixtures():
    normals = [parse_bar(r) for r in load("bars_normal_7203")]
    assert all(classify(b) is RowStatus.NORMAL for b in normals)

    no_trades = [parse_bar(r) for r in load("bars_no_trade_sample") if r["O"] is None]
    assert all(classify(b) is RowStatus.NO_TRADE for b in no_trades)


@pytest.mark.parametrize(
    ("close", "adj", "vol", "expected"),
    [
        (Decimal(100), Decimal(100), Decimal(5), RowStatus.NORMAL),
        (None, None, None, RowStatus.NO_TRADE),
        # 部分 null 的异常形态（决策 1 追加的防御）
        (Decimal(100), None, Decimal(5), RowStatus.ANOMALOUS),
        (None, Decimal(100), Decimal(5), RowStatus.ANOMALOUS),
        (Decimal(100), Decimal(100), None, RowStatus.ANOMALOUS),
    ],
)
def test_classify_three_states(close, adj, vol, expected):
    bar = Bar(date(2026, 8, 6), "10010", close, adj, vol)
    assert classify(bar) is expected


# --- T2 取价回退：当日 NO_TRADE / 停牌 → 取最近 NORMAL 行 ------------------


def test_t2_latest_is_last_normal_row():
    bars = [parse_bar(r) for r in load("bars_normal_7203")]
    price, anomalies = pick_price(bars)
    assert anomalies == []
    assert price is not None
    last = bars[-1]
    assert price.as_of == last.trade_date
    assert price.latest_close == last.close


def test_t2_skips_trailing_no_trade():
    """尾部是无成交行时，回退到之前最近的 NORMAL 行，as_of 随之回退。"""
    normal = [parse_bar(r) for r in load("bars_normal_7203")][-3:]
    stale = Bar(date(2026, 8, 10), "10010", None, None, None)  # 尾部停牌
    price, _ = pick_price(normal + [stale])
    assert price is not None
    assert price.as_of == normal[-1].trade_date   # 不是 08-24
    assert price.latest_close == normal[-1].close


def test_t2_no_normal_rows_returns_none():
    bars = [Bar(date(2026, 8, d), "99999", None, None, None) for d in (19, 20, 21)]
    price, anomalies = pick_price(bars)
    assert price is None and anomalies == []


def test_t2_pct_change_uses_two_latest_normals():
    bars = [parse_bar(r) for r in load("bars_normal_7203")]
    price, _ = pick_price(bars)
    expected = bars[-1].adj_close / bars[-2].adj_close - Decimal(1)
    assert price.pct_change_today == expected


def test_t2_pct_change_none_when_single_row():
    bars = [parse_bar(load("bars_normal_7203")[0])]
    price, _ = pick_price(bars)
    assert price is not None and price.pct_change_today is None


# --- T3 跨拆股日：pct_change 用复权列，不出假值 ----------------------------


def test_t3_split_day_uses_adjusted_columns():
    """构造 1:25 拆股：原始价从 4405 跌到 171.2（-96%），复权价实际是 -2.8%。

    数值取自真实拆股事件形态（NTT 2023-06-29，AdjFactor=0.04）：
    源提供的复权价已把除权前价格按因子缩放，故复权价相除得真实涨跌。
    """
    before = Bar(date(2025, 6, 28), "10030", Decimal("2500"), Decimal("100.0"), Decimal(1))
    after = Bar(date(2025, 6, 29), "10030", Decimal("97.5"), Decimal("97.5"), Decimal(1))
    price, _ = pick_price([before, after])

    assert price is not None
    # 估值用原始价：拿到的是当日真实成交价
    assert price.latest_close == Decimal("97.5")
    # 当日涨跌用复权价：-2.8%，而不是原始价相除的 -96%
    assert price.pct_change_today == Decimal("97.5") / Decimal("100.0") - 1
    assert Decimal("-0.03") < price.pct_change_today < Decimal("-0.02")

    naive = Decimal("97.5") / Decimal("2500") - 1   # 若误用原始价
    assert naive < Decimal("-0.96")
    assert price.pct_change_today != naive


# --- T8 异常行不进入计算（Jack 追加）--------------------------------------


def test_t8_anomalous_row_never_becomes_latest():
    """部分 null 的异常行必须被跳过，latest 回退到之前的 NORMAL 行。"""
    good = Bar(date(2026, 8, 5), "10010", Decimal("1000"), Decimal("1000"), Decimal(1))
    anomalous = Bar(date(2026, 8, 6), "10010", Decimal("1010"), None, Decimal(1))

    price, anomalies = pick_price([good, anomalous])

    assert price is not None
    assert price.as_of == date(2026, 8, 5)          # 不是 08-21
    assert price.latest_close == Decimal("1000")
    assert len(anomalies) == 1                        # 供调用方 WARN
    assert anomalies[0].trade_date == date(2026, 8, 6)


def test_t8_anomalous_row_not_used_as_prev_either():
    """异常行也不能充当 prev——否则 pct_change 分母是残缺数据。"""
    older = Bar(date(2026, 8, 4), "10010", Decimal("990"), Decimal("990"), Decimal(1))
    anomalous = Bar(date(2026, 8, 5), "10010", None, Decimal("1000"), Decimal(1))
    latest = Bar(date(2026, 8, 6), "10010", Decimal("1010"), Decimal("1010"), Decimal(1))

    price, anomalies = pick_price([older, anomalous, latest])

    assert price.as_of == date(2026, 8, 6)
    # 分母应是 08-19 的 2941，而不是异常行的 3066
    assert price.pct_change_today == Decimal("1010") / Decimal("990") - 1
    assert len(anomalies) == 1
