"""交易日历（检查点 2 后续批复第 3 条）。

批复要求的测试：0/1/3 映射、未知值 null+WARN、日切缓存、降级路径、not_covered。
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from pnlapi import api as api_module
from pnlapi import calendar as cal_module
from pnlapi.calendar import CalendarService, map_holdiv
from pnlapi.clock import today_jst
from pnlapi.config import get_settings
from pnlapi.jquants import JQuantsClient

KEY = get_settings().api_key_local.get_secret_value()
AUTH = {"X-API-Key": KEY}
TODAY = today_jst()


def make_service(rows: list[dict] | Exception) -> CalendarService:
    def fetch(path: str, params: dict) -> list[dict]:
        if isinstance(rows, Exception):
            raise rows
        return rows
    return CalendarService(fetch)


# --- HolDiv 映射（批复：0/1/3 + 未知值）-----------------------------------


@pytest.mark.parametrize(
    ("holdiv", "expected"),
    [
        ("1", True),    # 営業日
        ("0", False),   # 非営業日
        ("3", False),   # 非営業日（OSE 祝日取引あり）——东证现货不开
        ("2", True),    # 東証半日立会日：仍开市
    ],
)
def test_holdiv_mapping(holdiv, expected):
    assert map_holdiv(holdiv) is expected


@pytest.mark.parametrize("unknown", ["9", "", "X", "10"])
def test_unknown_holdiv_returns_none_and_warns(unknown, caplog):
    """未知取值 → null + WARN，禁止猜。"""
    with caplog.at_level(logging.WARNING, logger="pnlapi.calendar"):
        assert map_holdiv(unknown) is None
    assert any("未知 HolDiv" in r.message for r in caplog.records)


def test_unknown_holdiv_propagates_as_null():
    svc = make_service([
        {"Date": TODAY.isoformat(), "HolDiv": "7"},
    ])
    assert svc.is_trading_day(TODAY) is None       # 未知 → null，不是 False


# --- 查询 ------------------------------------------------------------------


def test_is_trading_day_today():
    svc = make_service([{"Date": TODAY.isoformat(), "HolDiv": "1"}])
    assert svc.is_trading_day_today() is True

    svc = make_service([{"Date": TODAY.isoformat(), "HolDiv": "0"}])
    assert svc.is_trading_day_today() is False


def test_range_marks_not_covered():
    """超出源覆盖范围的日期显式 not_covered，不猜。"""
    svc = make_service([
        {"Date": TODAY.isoformat(), "HolDiv": "1"},
        {"Date": (TODAY + timedelta(days=1)).isoformat(), "HolDiv": "0"},
    ])
    rows = svc.range(TODAY - timedelta(days=1), TODAY + timedelta(days=2))
    assert len(rows) == 4

    assert rows[0]["not_covered"] is True          # 源里没有的过去日期
    assert rows[0]["is_trading_day"] is None
    assert rows[0]["holiday_division"] is None

    assert rows[1]["not_covered"] is False
    assert rows[1]["is_trading_day"] is True
    assert rows[1]["holiday_division"] == "1"      # 原值透传

    assert rows[2]["is_trading_day"] is False
    assert rows[3]["not_covered"] is True          # 超出未来覆盖


def test_coverage_reports_actual_range():
    svc = make_service([
        {"Date": "2026-08-20", "HolDiv": "1"},
        {"Date": "2026-08-24", "HolDiv": "1"},
    ])
    assert svc.coverage() == (date(2026, 8, 20), date(2026, 8, 24))


# --- 缓存：日切失效 --------------------------------------------------------


def test_calendar_cached_within_day():
    calls = []

    def fetch(path, params):
        calls.append(path)
        return [{"Date": TODAY.isoformat(), "HolDiv": "1"}]

    svc = CalendarService(fetch)
    for _ in range(5):
        svc.is_trading_day_today()
    assert len(calls) == 1, "同一天应只拉取一次"


def test_calendar_expires_on_date_rollover(monkeypatch):
    calls = []

    def fetch(path, params):
        calls.append(path)
        return [{"Date": TODAY.isoformat(), "HolDiv": "1"}]

    svc = CalendarService(fetch)
    svc.is_trading_day_today()
    assert len(calls) == 1

    # 日切
    from pnlapi import cache as cache_module
    tomorrow = TODAY + timedelta(days=1)
    monkeypatch.setattr(cache_module, "today_jst", lambda: tomorrow)
    monkeypatch.setattr(cal_module, "today_jst", lambda: tomorrow)
    svc.is_trading_day_today()
    assert len(calls) == 2, "日切后应重新拉取"


# --- 降级路径：绝不阻塞 ----------------------------------------------------


def test_fetch_failure_degrades_to_none(caplog):
    svc = make_service(RuntimeError("上游 500"))
    with caplog.at_level(logging.WARNING, logger="pnlapi.calendar"):
        assert svc.is_trading_day_today() is None
        assert svc.range(TODAY, TODAY) is None
        assert svc.coverage() is None
    assert any("拉取失败" in r.message for r in caplog.records)


def test_empty_response_degrades(caplog):
    svc = make_service([])
    with caplog.at_level(logging.WARNING, logger="pnlapi.calendar"):
        assert svc.is_trading_day_today() is None


def test_malformed_row_skipped_not_fatal(caplog):
    svc = make_service([
        {"Date": "not-a-date", "HolDiv": "1"},
        {"Date": TODAY.isoformat(), "HolDiv": "1"},
    ])
    with caplog.at_level(logging.WARNING, logger="pnlapi.calendar"):
        assert svc.is_trading_day_today() is True   # 坏行跳过，好行仍生效


# --- 端点 ------------------------------------------------------------------


def build_client(calendar_rows=None, *, calendar_fails: bool = False) -> JQuantsClient:
    rows = calendar_rows if calendar_rows is not None else [
        {"Date": TODAY.isoformat(), "HolDiv": "1"}
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/markets/calendar":
            if calendar_fails:
                return httpx.Response(500, text="calendar down")
            return httpx.Response(200, json={"data": rows})
        if path == "/v2/equities/master":
            return httpx.Response(200, json={"data": [
                {"Code": "72030", "CoName": "トヨタ自動車"}
            ]})
        if path == "/v2/equities/bars/daily":
            return httpx.Response(200, json={"data": [{
                "Date": "2026-08-21", "Code": "72030", "C": 3132.0,
                "AdjC": 3132.0, "Vo": 25924500.0,
            }]})
        return httpx.Response(404)

    return JQuantsClient(transport=httpx.MockTransport(handler),
                         sleeper=lambda _: None)


def make_tc(jq: JQuantsClient) -> TestClient:
    app = api_module.create_app()
    app.dependency_overrides[api_module.get_client] = lambda: jq
    return TestClient(app, raise_server_exceptions=False)


def test_meta_carries_calendar_fields():
    tc = make_tc(build_client())
    for path in ("/v1/health", "/v1/quotes?codes=7203"):
        meta = tc.get(path, headers=AUTH).json()["meta"]
        assert meta["today_jst"] == TODAY.isoformat()
        assert meta["is_trading_day_today"] is True


def test_meta_calendar_null_when_unavailable():
    """日历不可用 → meta 字段为 null，但请求照常成功（绝不阻塞）。"""
    tc = make_tc(build_client(calendar_fails=True))
    body = tc.get("/v1/quotes?codes=7203", headers=AUTH).json()
    assert body["meta"]["is_trading_day_today"] is None
    assert body["meta"]["today_jst"] == TODAY.isoformat()
    assert body["data"][0]["latest_close"] == 3132.0   # 盈亏路径不受影响


def test_calendar_endpoint():
    tc = make_tc(build_client([
        {"Date": TODAY.isoformat(), "HolDiv": "1"},
        {"Date": (TODAY + timedelta(days=1)).isoformat(), "HolDiv": "3"},
    ]))
    r = tc.get(f"/v1/calendar?from={TODAY}&to={TODAY + timedelta(days=2)}",
               headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 3
    assert body["data"][0]["holiday_division"] == "1"
    assert body["data"][0]["is_trading_day"] is True
    assert body["data"][1]["holiday_division"] == "3"
    assert body["data"][1]["is_trading_day"] is False    # OSE 祝日取引不算东证交易日
    assert body["data"][2]["not_covered"] is True
    assert body["coverage_to"] == (TODAY + timedelta(days=1)).isoformat()


def test_calendar_endpoint_degrades_when_unavailable():
    tc = make_tc(build_client(calendar_fails=True))
    body = tc.get(f"/v1/calendar?from={TODAY}&to={TODAY}", headers=AUTH).json()
    assert body["data"][0]["not_covered"] is True
    assert body["data"][0]["is_trading_day"] is None
    assert body["coverage_from"] is None


def test_calendar_endpoint_validates_range():
    tc = make_tc(build_client())
    r = tc.get(f"/v1/calendar?from={TODAY}&to={TODAY - timedelta(days=1)}",
               headers=AUTH)
    assert r.status_code == 400

    r = tc.get(f"/v1/calendar?from=2020-01-01&to=2026-01-01", headers=AUTH)
    assert r.status_code == 400


def test_calendar_endpoint_requires_auth():
    tc = make_tc(build_client())
    assert tc.get(f"/v1/calendar?from={TODAY}&to={TODAY}").status_code == 401
