# pnl-api

日本股票持仓盈亏 API。输入持仓（代码 + 股数 + 成本），返回市值、盈亏金额、盈亏率与当日涨跌。

**无状态微服务**：不建数据库、不做采集管线、不落任何持仓数据。持仓归调用方保管。

---

## 这是日终数据

数据源是 [J-Quants](https://jpx-jquants.com/)（日本交易所集团官方），
**日终（EOD）服务，没有盘中实时价**。

- "当前价格" = 最新可得的**日线收盘价**
- 当天的收盘价约 **16:30 JST** 之后才发布
- 响应中的 `as_of` 就是该价格所属的交易日——调用方必须自己核对

全部接口与文档都诚实反映这一点，不会把日终数据包装成实时行情。

---

## 端点

| 端点 | 用途 |
|---|---|
| `GET /v1/health` | 服务与上游可达性 |
| `GET /v1/quotes?codes=` | 批量取价（最多 100 个代码） |
| `POST /v1/pnl` | 持仓盈亏（最多 100 笔） |
| `GET /v1/calendar?from&to` | 东证交易日历 |

完整接口契约见 **[docs/consumer-api.md](docs/consumer-api.md)**——
那份文档独立完备，只读它就能完成接入。

---

## 核心设计

**估值用原始价，当日涨跌用复权价，两者不混用。**

```
market_value     = latest_close × shares        ← 原始价（市场真实成交价）
pct_change_today = latest.AdjC / prev.AdjC − 1  ← 复权价（跨除权日才不出假值）
```

若涨跌幅误用原始价，一只 1:25 拆股的股票会显示为单日下跌 96%。

**其他约束**：

- 金额全程 `Decimal`，经 `str` 中转，不用 float 累积误差
- 证券代码按**字符串**处理（可含字母如 `130A`），禁止转数值或截断
- 无成交日**不造价**：回退到最近一个有成交的交易日，`as_of` 如实返回
- 单笔持仓出错**不拖垮整个请求**，`totals` 只汇总成功项并标 `partial: true`
- 盈亏为**毛值**，不含手续费与税金（`meta.fees_included` 恒为 `false`）

---

## 运行

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)，以及一个 J-Quants API key。

```bash
uv sync --all-groups
cp .env.example .env        # 填入 JQUANTS_API_KEY 与 API_KEY_LOCAL
make run                    # 启动服务（仅绑定 127.0.0.1）
make test                   # 77 项测试
```

服务**只绑定 `127.0.0.1`**，配置成其他地址会拒绝启动。

macOS 上可用 `launchd/com.jack.pnlapi.plist` 常驻（含 KeepAlive 自动重启）。

---

## 关于测试数据

`tests/fixtures/` 中的样本是**虚构数据**：字段结构、类型与 null 形态取自真实的
J-Quants V2 响应（因此测试仍有意义），但数值、证券代码与公司名均为编造。

原因是数据供应商条款不允许公开分发其行情数据。文档中的响应示例同理。

接入后请以你自己请求到的真实响应为准。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/consumer-api.md](docs/consumer-api.md) | 接口契约（调用方只读这一份即可） |
| [docs/runbook.md](docs/runbook.md) | 运维：启停、应急、配置项 |
| [docs/decisions.md](docs/decisions.md) | 决策日志与设计取舍 |
| [docs/fixture-notes.md](docs/fixture-notes.md) | 字段实测笔记与 NO_TRADE 判定依据 |
