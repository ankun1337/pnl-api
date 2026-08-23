"""三个端点（提示词第 8 节）。

数学全部调 pnl.py 纯函数，本层禁止重写。
命名与文案禁止出现 current / realtime / 实时——这是日终数据。
系统不落任何持仓数据，持仓归消费方保管。
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from pnlapi import pnl
from pnlapi.clock import now_jst
from pnlapi.config import get_settings
from pnlapi.jquants import JQuantsClient
from pnlapi.logsafe import setup_logging

logger = logging.getLogger("pnlapi.api")

MAX_CODES = 100
MAX_POSITIONS = 100


# --- 响应模型 --------------------------------------------------------------


class Meta(BaseModel):
    request_id: str
    generated_at: datetime
    source: Literal["JQUANTS_V2"] = "JQUANTS_V2"
    latency_class: Literal["END_OF_DAY"] = Field(
        default="END_OF_DAY",
        description="本服务提供日终收盘价。当日收盘价约 16:30 JST 后才发布",
    )
    fees_included: Literal[False] = Field(
        default=False, description="盈亏为毛值，不含手续费与税金"
    )


class ItemError(BaseModel):
    """持仓级 / 代码级错误。与 HTTP 层错误同形。"""

    code: str
    message: str
    hint: str


class Quote(BaseModel):
    code: str = Field(description="回显调用方传入的原样代码")
    name_ja: str | None
    latest_close: float | None = Field(description="最新可得收盘价（原始价）")
    as_of: date | None = Field(description="该收盘价所属交易日")
    pct_change_today: float | None = Field(
        description="较前一交易日的涨跌幅（复权价计算），小数"
    )
    error: ItemError | None = None


class QuotesResponse(BaseModel):
    meta: Meta
    data: list[Quote]


class PositionResult(BaseModel):
    code: str
    name_ja: str | None
    shares: int | None
    cost_total: float | None
    latest_close: float | None
    as_of: date | None
    market_value: float | None
    profit: float | None
    profit_rate: float | None
    pct_change_today: float | None
    error: ItemError | None = None


class Totals(BaseModel):
    cost_total: float
    market_value: float
    profit: float
    profit_rate: float | None
    partial: bool = Field(description="存在失败持仓时为 true，totals 只汇总成功项")


class PnlResponse(BaseModel):
    meta: Meta
    data: list[PositionResult]
    totals: Totals


class HealthData(BaseModel):
    ok: bool
    jquants_reachable: bool


class HealthResponse(BaseModel):
    meta: Meta
    data: HealthData


# --- 请求模型（结构性错误在此拦截 → 整体 400，见 decisions.md 错误层级）---


class Position(BaseModel):
    code: str = Field(min_length=1, max_length=12)
    shares: int
    cost_price: Decimal | None = None
    cost_total: Decimal | None = None

    @model_validator(mode="after")
    def _cost_exactly_one(self) -> "Position":
        if (self.cost_price is None) == (self.cost_total is None):
            raise ValueError(
                "cost_price 与 cost_total 必须且只能提供一个"
            )
        return self


class PnlRequest(BaseModel):
    positions: list[Position] = Field(min_length=1, max_length=MAX_POSITIONS)


# --- 依赖 ------------------------------------------------------------------


def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    expected = get_settings().api_key_local.get_secret_value()
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "UNAUTHORIZED",
                "message": "缺少或错误的 X-API-Key 请求头",
                "hint": "检查 .env 中的 API_KEY_LOCAL 是否与请求头一致",
            },
        )


Auth = Annotated[None, Depends(require_api_key)]

_client: JQuantsClient | None = None


def get_client() -> JQuantsClient:
    """进程内单例——缓存挂在客户端上，换实例等于丢缓存。"""
    global _client
    if _client is None:
        _client = JQuantsClient()
    return _client


Client = Annotated[JQuantsClient, Depends(get_client)]


def _meta() -> Meta:
    return Meta(request_id=str(uuid.uuid4()), generated_at=now_jst())


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


# --- 服务层：取价（去重仅限此处，决策 4）----------------------------------


class Outcome(BaseModel):
    """一个代码的取价结局：要么有价，要么有错。"""

    model_config = {"arbitrary_types_allowed": True}

    name_ja: str | None = None
    latest_close: Decimal | None = None
    as_of: date | None = None
    pct_change_today: Decimal | None = None
    error: ItemError | None = None


def resolve_codes(client: JQuantsClient, user_codes: list[str]) -> dict[str, Outcome]:
    """user_code → Outcome。**同一代码只向 J-Quants 请求一次**。"""
    results: dict[str, Outcome] = {}
    for user_code in dict.fromkeys(user_codes):  # 去重且保序
        results[user_code] = _resolve_one(client, user_code)
    return results


def _resolve_one(client: JQuantsClient, user_code: str) -> Outcome:
    vendor_code, master_ok = client.resolve_code(user_code)

    if vendor_code is None:
        return Outcome(
            error=ItemError(
                code=pnl.ERR_UNKNOWN_CODE,
                message=f"代码 {user_code} 不存在于上市证券主数据中",
                hint="确认代码是否正确；已退市证券也会命中此错误",
            )
        )

    try:
        bars = client.fetch_bars(vendor_code)
    except Exception as exc:  # noqa: BLE001 - 单代码失败不拖垮整个请求
        logger.warning("取价失败 code=%s: %s", user_code, exc)
        return Outcome(
            name_ja=client.name_of(vendor_code),
            error=ItemError(
                code=pnl.ERR_NO_RECENT_PRICE,
                message=f"代码 {user_code} 的行情取得失败",
                hint="稍后重试；若持续失败请检查上游数据源可达性",
            ),
        )

    price, anomalies = pnl.pick_price(bars)
    for bad in anomalies:
        # 决策 1 追加：部分 null 的异常行跳过并 WARN
        logger.warning(
            "跳过异常行 code=%s date=%s（部分字段为 null，不得用于计算）",
            vendor_code, bad.trade_date,
        )

    name = client.name_of(vendor_code)
    if price is None:
        hint = "回看窗口内无可用成交记录，可能长期停牌"
        if not master_ok:
            hint = pnl.HINT_MASTER_UNAVAILABLE  # 决策 3 的降级
        return Outcome(
            name_ja=name,
            error=ItemError(
                code=pnl.ERR_NO_RECENT_PRICE,
                message=f"代码 {user_code} 在回看窗口内没有可用的收盘价",
                hint=hint,
            ),
        )

    return Outcome(
        name_ja=name,
        latest_close=price.latest_close,
        as_of=price.as_of,
        pct_change_today=price.pct_change_today,
    )


# --- 应用 ------------------------------------------------------------------


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="股票盈亏 API",
        version="1.0.0",
        description=(
            "输入持仓，返回市值与盈亏。**日终数据**：价格为最新可得的日线收盘价，"
            "当日收盘价约 16:30 JST 后才发布。响应中的 as_of 即该价格所属交易日，"
            "消费方须自行核对 as_of 是否为预期日期。"
        ),
    )

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        payload = detail if isinstance(detail, dict) and "code" in detail else {
            "code": f"HTTP_{exc.status_code}",
            "message": str(detail),
            "hint": "参见 docs/consumer-api.md 的错误目录",
        }
        return JSONResponse(status_code=exc.status_code, content={"error": payload})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": f"请求不合法（{problems}）",
                    "hint": "cost_price 与 cost_total 必须且只能给一个；"
                            "对照 docs/consumer-api.md 的参数表检查",
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务内部错误",
                    "hint": "查看服务日志定位；日志已做密钥脱敏",
                }
            },
        )

    @app.get("/v1/health", response_model=HealthResponse, summary="健康检查")
    def health(client: Client, _: Auth) -> HealthResponse:
        return HealthResponse(
            meta=_meta(),
            data=HealthData(ok=True, jquants_reachable=client.reachable()),
        )

    @app.get("/v1/quotes", response_model=QuotesResponse, summary="批量取价")
    def quotes(
        client: Client,
        _: Auth,
        codes: Annotated[str, Query(description="逗号分隔的证券代码，最多 100 个")],
    ) -> QuotesResponse:
        items = [c.strip() for c in codes.split(",") if c.strip()]
        if not items:
            raise HTTPException(400, {
                "code": "INVALID_REQUEST", "message": "codes 不能为空",
                "hint": "形如 codes=7203,6758",
            })
        if len(items) > MAX_CODES:
            raise HTTPException(400, {
                "code": "TOO_MANY_CODES",
                "message": f"codes 数量 {len(items)} 超过上限 {MAX_CODES}",
                "hint": "分批调用",
            })
        outcomes = resolve_codes(client, items)
        return QuotesResponse(
            meta=_meta(),
            data=[
                Quote(
                    code=c,
                    name_ja=outcomes[c].name_ja,
                    latest_close=_f(outcomes[c].latest_close),
                    as_of=outcomes[c].as_of,
                    pct_change_today=_f(outcomes[c].pct_change_today),
                    error=outcomes[c].error,
                )
                for c in dict.fromkeys(items)
            ],
        )

    @app.post("/v1/pnl", response_model=PnlResponse, summary="持仓盈亏")
    def compute(client: Client, _: Auth, body: PnlRequest) -> PnlResponse:
        outcomes = resolve_codes(client, [p.code for p in body.positions])

        rows: list[PositionResult] = []
        successes: list[pnl.PnlResult] = []
        had_error = False

        # 决策 4：同码多笔持仓逐行返回，不合并
        for position in body.positions:
            outcome = outcomes[position.code]
            base: dict[str, Any] = {
                "code": position.code,
                "name_ja": outcome.name_ja,
                "shares": position.shares,
                "cost_total": None,
                "latest_close": _f(outcome.latest_close),
                "as_of": outcome.as_of,
                "market_value": None,
                "profit": None,
                "profit_rate": None,
                "pct_change_today": _f(outcome.pct_change_today),
            }

            if outcome.error is not None:
                had_error = True
                rows.append(PositionResult(**base, error=outcome.error))
                continue

            if err := pnl.validate_shares(position.shares):
                had_error = True
                rows.append(PositionResult(**base, error=ItemError(
                    code=err, message="shares 必须是正整数",
                    hint="传入当前持有股数，整数且大于 0",
                )))
                continue

            cost_total, err = pnl.resolve_cost_total(
                position.shares, position.cost_price, position.cost_total
            )
            if err:
                had_error = True
                rows.append(PositionResult(**base, error=ItemError(
                    code=err, message="成本参数不合法",
                    hint="cost_price 或 cost_total 二选一，且必须为正数",
                )))
                continue

            result = pnl.compute_pnl(
                position.shares, cost_total, outcome.latest_close  # type: ignore[arg-type]
            )
            successes.append(result)
            rows.append(PositionResult(**{
                **base,
                "cost_total": _f(result.cost_total),
                "market_value": _f(result.market_value),
                "profit": _f(result.profit),
                "profit_rate": _f(result.profit_rate),
            }))

        totals = pnl.aggregate(successes, had_error)
        return PnlResponse(
            meta=_meta(),
            data=rows,
            totals=Totals(
                cost_total=float(totals["cost_total"]),
                market_value=float(totals["market_value"]),
                profit=float(totals["profit"]),
                profit_rate=_f(totals["profit_rate"]),
                partial=totals["partial"],
            ),
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    if settings.api_bind != "127.0.0.1":
        raise SystemExit(f"拒绝启动：API_BIND={settings.api_bind!r}，只允许 127.0.0.1")
    logger.info("服务启动于 http://%s:%s", settings.api_bind, settings.api_port)
    uvicorn.run(app, host=settings.api_bind, port=settings.api_port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
