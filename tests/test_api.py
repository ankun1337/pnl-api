"""T4–T7、T9：成本传参、错误隔离、未知代码、日志无 key、同码多笔。

用 MockTransport 喂真实 fixture 内容，不发任何网络请求。
"""

from __future__ import annotations

import io
import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pnlapi import api as api_module
from pnlapi import logsafe
from pnlapi.config import get_settings
from pnlapi.jquants import JQuantsClient

FIXTURES = Path(__file__).parent / "fixtures"
KEY = get_settings().api_key_local.get_secret_value()
AUTH = {"X-API-Key": KEY}


def fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


NORMAL_ROWS = fixture("bars_normal_7203")
MASTER_ROWS = fixture("master_names")
NO_TRADE_ROWS = [r for r in fixture("bars_no_trade_sample") if r["O"] is None]

# 用真实 fixture 的代码构造受控宇宙
CODE_OK = "1001"           # 调用方写法（4 位）
VENDOR_OK = "10010"
CODE_HALTED = NO_TRADE_ROWS[0]["Code"]   # 全窗口无成交的代码


def build_client(*, master_fails: bool = False) -> JQuantsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/equities/master":
            if master_fails:
                return httpx.Response(500, text="master down")
            rows = list(MASTER_ROWS)
            # 确保受控代码在主数据中
            rows.append({"Code": VENDOR_OK, "CoName": "架空重工業"})
            rows.append({"Code": CODE_HALTED, "CoName": "架空停牌銘柄"})
            return httpx.Response(200, json={"data": rows})
        if path == "/v2/equities/bars/daily":
            code = request.url.params.get("code")
            if code == VENDOR_OK:
                return httpx.Response(200, json={"data": NORMAL_ROWS})
            if code == CODE_HALTED:
                return httpx.Response(200, json={"data": NO_TRADE_ROWS})
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, text="unexpected path")

    return JQuantsClient(
        transport=httpx.MockTransport(handler), sleeper=lambda _: None
    )


def make_test_client(jq: JQuantsClient) -> TestClient:
    """只用 dependency_overrides 注入。

    注意不要同时 monkeypatch api_module.get_client——路由的 Depends 在模块导入
    时就绑定了原始函数对象，monkeypatch 会让 overrides 的 key 对不上，
    覆盖静默失效（本测试文件曾因此误报去重未生效）。
    """
    app = api_module.create_app()
    app.dependency_overrides[api_module.get_client] = lambda: jq
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client() -> TestClient:
    return make_test_client(build_client())


def post_pnl(client: TestClient, positions: list[dict]) -> httpx.Response:
    return client.post("/v1/pnl", json={"positions": positions}, headers=AUTH)


# --- 基础契约 --------------------------------------------------------------


def test_meta_present_and_honest(client):
    """meta 固定携带日终标识与费税免责（提示词第 8 节）。"""
    for response in (
        client.get("/v1/health", headers=AUTH),
        client.get(f"/v1/quotes?codes={CODE_OK}", headers=AUTH),
        post_pnl(client, [{"code": CODE_OK, "shares": 100, "cost_price": 3000}]),
    ):
        assert response.status_code == 200, response.text
        meta = response.json()["meta"]
        assert meta["source"] == "JQUANTS_V2"
        assert meta["latency_class"] == "END_OF_DAY"
        assert meta["fees_included"] is False
        assert meta["request_id"] and "+09:00" in meta["generated_at"]


def test_auth_required(client):
    for path in ("/v1/health", f"/v1/quotes?codes={CODE_OK}"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"
    r = client.post("/v1/pnl", json={"positions": []})
    assert r.status_code == 401


def test_no_realtime_wording_in_openapi(client):
    """红线：一切命名与文案禁止出现 current / realtime / 实时。"""
    import re

    spec = client.get("/openapi.json").text
    assert not re.search(r"(?i)\b(real[\s_-]?time|current)\b", spec)
    assert "实时" not in spec


# --- T4 成本传参：双给 / 双缺 → 400 ---------------------------------------


def test_t4_both_cost_fields_rejected(client):
    r = post_pnl(client, [
        {"code": CODE_OK, "shares": 100, "cost_price": 3000, "cost_total": 300000}
    ])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_t4_neither_cost_field_rejected(client):
    r = post_pnl(client, [{"code": CODE_OK, "shares": 100}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_t4_cost_price_and_cost_total_equivalent(client):
    """cost_price × shares 与直接给 cost_total 结果一致。"""
    a = post_pnl(client, [{"code": CODE_OK, "shares": 300, "cost_price": 3000}])
    b = post_pnl(client, [{"code": CODE_OK, "shares": 300, "cost_total": 900000}])
    assert a.status_code == b.status_code == 200
    assert a.json()["data"][0]["profit"] == b.json()["data"][0]["profit"]
    assert a.json()["data"][0]["cost_total"] == 900000.0


# --- 盈亏数学正确性（"算对钱高于一切"）------------------------------------


def test_pnl_math_matches_hand_calculation(client):
    latest_close = Decimal(str(NORMAL_ROWS[-1]["C"]))
    shares, cost_price = 300, Decimal("3000")

    r = post_pnl(client, [
        {"code": CODE_OK, "shares": shares, "cost_price": float(cost_price)}
    ])
    row = r.json()["data"][0]

    expected_cost = cost_price * shares
    expected_value = latest_close * shares
    expected_profit = expected_value - expected_cost

    assert row["cost_total"] == float(expected_cost)
    assert row["market_value"] == float(expected_value)
    assert row["profit"] == float(expected_profit)
    assert row["profit_rate"] == pytest.approx(float(expected_profit / expected_cost))
    assert row["as_of"] == NORMAL_ROWS[-1]["Date"]      # 原样返回
    assert row["latest_close"] == float(latest_close)   # 估值用原始价


def test_valuation_uses_raw_not_adjusted(client):
    """估值必须用原始价 C，不能用 AdjC（两者在拆股后会分叉）。"""
    r = client.get(f"/v1/quotes?codes={CODE_OK}", headers=AUTH)
    quote = r.json()["data"][0]
    assert quote["latest_close"] == float(NORMAL_ROWS[-1]["C"])


# --- T5 错误项隔离：一只错、其余正常 --------------------------------------


def test_t5_error_isolation_and_totals(client):
    r = post_pnl(client, [
        {"code": CODE_OK, "shares": 100, "cost_price": 3000},
        {"code": "00000", "shares": 100, "cost_price": 1000},    # 未知代码
        {"code": CODE_OK, "shares": 200, "cost_price": 3100},
    ])
    assert r.status_code == 200
    body = r.json()
    rows, totals = body["data"], body["totals"]

    assert len(rows) == 3
    assert rows[0]["error"] is None and rows[2]["error"] is None
    assert rows[1]["error"]["code"] == "UNKNOWN_CODE"
    assert rows[1]["profit"] is None                  # 失败项不编造数字

    # totals 只汇总成功项
    assert totals["partial"] is True
    assert totals["cost_total"] == 100 * 3000 + 200 * 3100
    assert totals["profit"] == pytest.approx(
        rows[0]["profit"] + rows[2]["profit"]
    )


def test_t5_all_success_partial_false(client):
    r = post_pnl(client, [{"code": CODE_OK, "shares": 100, "cost_price": 3000}])
    assert r.json()["totals"]["partial"] is False


def test_t5_invalid_shares_is_item_level(client):
    """shares 非正整数是持仓级错误，不拖垮整个请求。"""
    r = post_pnl(client, [
        {"code": CODE_OK, "shares": 100, "cost_price": 3000},
        {"code": CODE_OK, "shares": 0, "cost_price": 3000},
    ])
    assert r.status_code == 200
    rows = r.json()["data"]
    assert rows[0]["error"] is None
    assert rows[1]["error"]["code"] == "INVALID_SHARES"
    assert r.json()["totals"]["partial"] is True


# --- T6 未知代码 -----------------------------------------------------------


def test_t6_unknown_code(client):
    r = client.get("/v1/quotes?codes=00000", headers=AUTH)
    quote = r.json()["data"][0]
    assert quote["error"]["code"] == "UNKNOWN_CODE"
    assert quote["latest_close"] is None and quote["as_of"] is None


def test_t6_halted_code_gets_no_recent_price(client):
    """代码存在但窗口内无成交 → NO_RECENT_PRICE（区别于 UNKNOWN_CODE）。"""
    r = client.get(f"/v1/quotes?codes={CODE_HALTED}", headers=AUTH)
    quote = r.json()["data"][0]
    assert quote["error"]["code"] == "NO_RECENT_PRICE"


def test_t6_master_unavailable_degrades():
    """主数据不可用 → 仍用 NO_RECENT_PRICE，hint 注明未验证（决策 3）。"""
    tc = make_test_client(build_client(master_fails=True))

    quote = tc.get("/v1/quotes?codes=99999", headers=AUTH).json()["data"][0]
    assert quote["error"]["code"] == "NO_RECENT_PRICE"   # 不是 UNKNOWN_CODE
    assert "主数据不可用" in quote["error"]["hint"]
    assert quote["name_ja"] is None                      # 名称缺失不阻塞


# --- T9 同码多笔持仓逐行返回（Jack 追加）----------------------------------


def test_t9_same_code_multiple_positions(client):
    """决策 4：去重仅限取数层，同码多笔在响应中逐行返回，不合并。"""
    r = post_pnl(client, [
        {"code": CODE_OK, "shares": 100, "cost_price": 3000},
        {"code": CODE_OK, "shares": 200, "cost_price": 2800},
        {"code": CODE_OK, "shares": 50, "cost_total": 160000},
    ])
    body = r.json()
    rows, totals = body["data"], body["totals"]

    assert len(rows) == 3, "同码多笔必须逐行返回"
    assert all(row["code"] == CODE_OK for row in rows)
    assert [row["shares"] for row in rows] == [100, 200, 50]
    # 三行成本各不相同，未被合并
    assert rows[0]["cost_total"] == 300000.0
    assert rows[1]["cost_total"] == 560000.0
    assert rows[2]["cost_total"] == 160000.0

    # totals 是三笔之和
    assert totals["cost_total"] == pytest.approx(300000 + 560000 + 160000)
    assert totals["market_value"] == pytest.approx(sum(r["market_value"] for r in rows))
    assert totals["profit"] == pytest.approx(sum(r["profit"] for r in rows))
    assert totals["partial"] is False


def test_t9_dedup_only_at_fetch_layer():
    """同码多笔只向上游请求一次（决策 4：去重仅限取数层）。"""
    calls: list[str] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/equities/master":
            rows = list(MASTER_ROWS) + [{"Code": VENDOR_OK, "CoName": "架空重工業"}]
            return httpx.Response(200, json={"data": rows})
        calls.append(request.url.params.get("code"))
        return httpx.Response(200, json={"data": NORMAL_ROWS})

    tc = make_test_client(JQuantsClient(
        transport=httpx.MockTransport(counting_handler), sleeper=lambda _: None
    ))

    response = tc.post("/v1/pnl", headers=AUTH, json={"positions": [
        {"code": CODE_OK, "shares": 100, "cost_price": 3000},
        {"code": CODE_OK, "shares": 200, "cost_price": 3000},
        {"code": CODE_OK, "shares": 300, "cost_price": 3000},
    ]})
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 3          # 逐行返回
    assert calls.count(VENDOR_OK) == 1, f"同码应只请求一次，实际 {calls}"


def test_code_echoed_as_given(client):
    """决策 4：响应 code 回显调用方传入的原样值。"""
    r = client.get(f"/v1/quotes?codes={CODE_OK},{VENDOR_OK}", headers=AUTH)
    codes = [q["code"] for q in r.json()["data"]]
    assert codes == [CODE_OK, VENDOR_OK]   # 4 位与 5 位各自原样回显


# --- 上限 ------------------------------------------------------------------


def test_too_many_codes(client):
    codes = ",".join(str(1000 + i) for i in range(101))
    r = client.get(f"/v1/quotes?codes={codes}", headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TOO_MANY_CODES"


def test_too_many_positions(client):
    positions = [{"code": CODE_OK, "shares": 1, "cost_price": 1} for _ in range(101)]
    r = post_pnl(client, positions)
    assert r.status_code == 400


# --- T7 日志无 key ---------------------------------------------------------


def test_t7_api_key_never_in_logs(client, monkeypatch):
    jq_key = get_settings().jquants_api_key.get_secret_value()
    monkeypatch.setattr(logsafe, "secret_values", lambda: [KEY, jq_key])

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(logsafe.RedactingFilter())
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    root.handlers, root.level = [handler], logging.DEBUG
    try:
        client.get("/v1/health", headers=AUTH)
        client.get(f"/v1/quotes?codes={CODE_OK}", headers=AUTH)
        post_pnl(client, [{"code": "00000", "shares": 1, "cost_price": 1}])
        logging.getLogger("probe").info("x-api-key: %s", jq_key)
        logging.getLogger("probe").info("本地 key %s", KEY)
    finally:
        root.handlers, root.level = old_handlers, old_level

    output = stream.getvalue()
    assert jq_key not in output
    assert KEY not in output
    assert logsafe.REDACTED in output


def test_internal_error_leaks_nothing(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(f"内部细节泄露 x-api-key: {KEY}")

    monkeypatch.setattr(api_module, "resolve_codes", boom)
    r = client.get(f"/v1/quotes?codes={CODE_OK}", headers=AUTH)
    assert r.status_code == 500
    assert KEY not in r.text
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
