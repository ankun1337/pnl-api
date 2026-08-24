# 消费方接入文档

> **本文档中的所有行情数值、证券代码与公司名均为虚构示例。**
> 字段结构、类型与 null 形态取自真实 J-Quants V2 响应，可据此编码；
> 但数值本身不代表任何真实市场数据——数据供应商条款不允许公开分发其数据。
> 接入后请以你自己请求到的真实响应为准。

**股票盈亏 API** 的接口合同。你只需要这一份文档就能完成接入，不需要读源码。

---

## 1. 这是日终数据，不是盘中行情

**最重要的一件事：本服务返回的是最新可得的日线收盘价。**

- 数据源是 J-Quants（日本交易所集团官方），**日终（EOD）服务，没有盘中价**
- **当天的收盘价约 16:30 JST 之后才发布**
- 响应中的 `as_of` 就是该价格所属的交易日——**你必须自己核对它**

所以：

| 你调用的时刻 | 你拿到的 `as_of` |
|---|---|
| 交易日 16:30 JST 之后 | 通常是当天 |
| 交易日 16:30 JST 之前 | **前一个交易日** |
| 周末 / 节假日 | 最近一个交易日 |
| 该股停牌 | 停牌前最后一个有成交的交易日 |

**禁止把旧 `as_of` 的数字当作"今日盈亏"发布。** 见第 6 节的就绪自查协议。

---

## 2. 接入基础

| 项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:<API_PORT>`，`API_PORT` 默认 **8642** |
| 鉴权 | 请求头 `X-API-Key` |
| 配置文件 | `~/projects/pnl-api/.env`（项目根目录下） |
| Key 键名 | `API_KEY_LOCAL` |
| 端口键名 | `API_PORT`（可选，缺省 8642） |

服务只绑定 `127.0.0.1`，仅本机可访问。本文档其余示例为可读性直接写 `8642`。

```python
from pathlib import Path
import httpx

PROJECT_ROOT = Path.home() / "projects/pnl-api"   # 项目若不在此处，改这一行

def load_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip().strip('"').strip("'")   # 容忍引号
    return values

ENV = load_env(PROJECT_ROOT / ".env")
BASE_URL = f"http://127.0.0.1:{ENV.get('API_PORT', '8642')}"
HEADERS = {"X-API-Key": ENV["API_KEY_LOCAL"]}
```

**不要把 key 写进代码、日志或提交。**

> **两个本机环境坑**：
>
> 1. **`.env` 的值不要加引号**。若写成 `API_KEY_LOCAL="xxx"`，引号会被当作 key 的
>    一部分送进请求头，得到 401。上面的 `load_env` 已做容错，自己写解析时要注意。
> 2. **系统级 HTTP 代理会拦截 127.0.0.1**。若本机配了全局代理（Shadowrocket 等），
>    `urllib`/`requests` 会自动继承它，导致连不上本地服务。
>    `httpx` 只读环境变量不读 macOS 系统配置，**不受影响**——这是推荐用 `httpx` 的原因。
>    若必须用标准库：
>
>    ```python
>    import urllib.request
>    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过代理
>    ```

---

## 3. 所有响应都带 meta

```json
{
  "request_id": "fd67f049-7b21-4521-89a6-cef38b707283",
  "generated_at": "2026-08-08T16:36:23.285772+09:00",
  "source": "JQUANTS_V2",
  "latency_class": "END_OF_DAY",
  "fees_included": false
}
```

| 字段 | 含义 |
|---|---|
| `request_id` | 本次请求唯一 ID，排查时报给运维。**只在成功响应中**，错误响应只有 `error` |
| `generated_at` | 响应生成时刻，JST 带时区偏移 |
| `source` | 恒为 `JQUANTS_V2` |
| `latency_class` | 恒为 `END_OF_DAY`。提醒你：这是日终数据，**不要在文案里称其为实时行情** |
| `fees_included` | 恒为 `false`。**盈亏是毛值，不含手续费与税金** |
| `today_jst` | 服务端当前的 JST 日期。**用它而不是你本地的日期**来判断 `as_of` 是否为"今天" |
| `is_trading_day_today` | 今天是否为东证交易日。`true`/`false`/**`null`** |

> **`is_trading_day_today` 为 `null` 表示"未知"，不表示"非交易日"**。
> 出现 `null` 的原因是日历数据暂时不可用——此时退回按星期几粗判即可，
> 盈亏计算完全不受影响。

---

## 4. 端点

四个端点，都需要 `X-API-Key` 头。

### 4.1 `GET /v1/health`

无参数。确认服务与上游可达。

```bash
curl -s -H "X-API-Key: $API_KEY" "http://127.0.0.1:8642/v1/health"
```

```python
r = httpx.get(f"{BASE_URL}/v1/health", headers=HEADERS, timeout=30)
r.raise_for_status()
print(r.json()["data"])
```

真实响应：

```json
{
  "meta": { "...": "见第 3 节" },
  "data": {
    "ok": true,
    "jquants_reachable": true
  }
}
```

> `jquants_reachable` 为 `false` 时，取价会失败。通常是网络或上游问题。

---

### 4.2 `GET /v1/quotes` — 批量取价

| 参数 | 类型 | 约束 |
|---|---|---|
| `codes` | string | 必填。逗号分隔，**最多 100 个** |

**代码写法**：四位习惯代码（`1001`）与五位供应商代码（`10010`）都可以，
响应中的 `code` 会**原样回显你传入的写法**。

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8642/v1/quotes?codes=1001,1002,00000"
```

```python
r = httpx.get(f"{BASE_URL}/v1/quotes",
              params={"codes": "1001,1002"}, headers=HEADERS, timeout=60)
for q in r.json()["data"]:
    if q["error"]:
        print(f"{q['code']}: {q['error']['code']}")
    else:
        print(f"{q['code']} {q['name_ja']} {q['latest_close']} @{q['as_of']}")
```

真实响应（含一个错误项）：

```json
{
  "meta": { "...": "见第 3 节" },
  "data": [
    {
      "code": "1001",
      "name_ja": "架空重工業",
      "latest_close": 1010.0,
      "as_of": "2026-08-06",
      "pct_change_today": 0.00990,
      "error": null
    },
    {
      "code": "1002",
      "name_ja": "架空電機",
      "latest_close": 1250.0,
      "as_of": "2026-08-06",
      "pct_change_today": 0.00402,
      "error": null
    },
    {
      "code": "00000",
      "name_ja": null,
      "latest_close": null,
      "as_of": null,
      "pct_change_today": null,
      "error": {
        "code": "UNKNOWN_CODE",
        "message": "代码 00000 不存在于上市证券主数据中",
        "hint": "确认代码是否正确；已退市证券也会命中此错误"
      }
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `latest_close` | 最新可得收盘价（**原始价**，即市场真实成交价） |
| `as_of` | 该价格所属交易日。**务必核对** |
| `pct_change_today` | 较前一交易日的涨跌幅，**小数**（`0.0215` = +0.99%）。用复权价计算，跨除权日不会出假值 |
| `error` | 成功时为 `null`；失败时见第 5 节 |

---

### 4.3 `POST /v1/pnl` — 持仓盈亏

请求体：

```json
{
  "positions": [
    {"code": "1001", "shares": 300, "cost_price": 800},
    {"code": "1002", "shares": 200, "cost_total": 200000}
  ]
}
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `positions` | array | 必填，1–**100** 条 |
| `code` | string | 必填。四位或五位均可 |
| `shares` | int | 必填。**正整数**，当前持有股数 |
| `cost_price` | number | 每股成本。与 `cost_total` **二选一** |
| `cost_total` | number | 总成本。与 `cost_price` **二选一** |

> **`cost_price` 与 `cost_total` 必须且只能给一个**，双给或双缺都会返回 400。

**同一代码可以传多笔**（分批建仓），系统会**逐笔返回**，不合并。

> **顺序合同**：`data[]` 与你请求的 `positions[]` **一一对应、顺序保持不变**。
> 即 `data[i]` 永远是 `positions[i]` 的结果，包括失败项也占位。
> 同代码多笔时，靠下标区分是哪一笔（响应中没有独立的索引字段）。

**请求头必须带 `Content-Type: application/json`**（httpx 的 `json=` 参数会自动带上；
用标准库需自己设置，否则返回 400）。

```bash
curl -s -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"positions":[{"code":"1001","shares":300,"cost_price":800}]}' \
  "http://127.0.0.1:8642/v1/pnl"
```

```python
r = httpx.post(f"{BASE_URL}/v1/pnl", headers=HEADERS, timeout=60, json={
    "positions": [
        {"code": "1001", "shares": 300, "cost_price": 800},
        {"code": "1002", "shares": 200, "cost_total": 200000},
    ]
})
body = r.json()
for row in body["data"]:
    if row["error"]:
        print(f"{row['code']}: {row['error']['code']}")
    else:
        print(f"{row['code']} 盈亏 {row['profit']:+,.0f} ({row['profit_rate']:+.2%})")
print("合计", body["totals"]["profit"], "partial:", body["totals"]["partial"])
```

真实响应（**成功与错误持仓混合**）：

```json
{
  "meta": {
    "request_id": "fd67f049-7b21-4521-89a6-cef38b707283",
    "generated_at": "2026-08-08T16:36:23.285772+09:00",
    "source": "JQUANTS_V2",
    "latency_class": "END_OF_DAY",
    "fees_included": false
  },
  "data": [
    {
      "code": "1001",
      "name_ja": "架空重工業",
      "shares": 300,
      "cost_total": 240000.0,
      "latest_close": 1010.0,
      "as_of": "2026-08-06",
      "market_value": 303000.0,
      "profit": 63000.0,
      "profit_rate": 0.26250,
      "pct_change_today": 0.00990,
      "error": null
    },
    {
      "code": "1002",
      "name_ja": "架空電機",
      "shares": 200,
      "cost_total": 200000.0,
      "latest_close": 1250.0,
      "as_of": "2026-08-06",
      "market_value": 250000.0,
      "profit": 50000.0,
      "profit_rate": 0.25000,
      "pct_change_today": 0.00402,
      "error": null
    },
    {
      "code": "00000",
      "name_ja": null,
      "shares": 100,
      "cost_total": null,
      "latest_close": null,
      "as_of": null,
      "market_value": null,
      "profit": null,
      "profit_rate": null,
      "pct_change_today": null,
      "error": {
        "code": "UNKNOWN_CODE",
        "message": "代码 00000 不存在于上市证券主数据中",
        "hint": "确认代码是否正确；已退市证券也会命中此错误"
      }
    }
  ],
  "totals": {
    "cost_total": 440000.0,
    "market_value": 553000.0,
    "profit": 113000.0,
    "profit_rate": 0.25682,
    "partial": true
  }
}
```

**计算口径**：

```
cost_total   = 传入的 cost_total，或 cost_price × shares
market_value = latest_close × shares          ← 用原始价估值
profit       = market_value − cost_total
profit_rate  = profit / cost_total
```

**`totals` 的三条规则**：

1. **只汇总成功的持仓**——上例中失败的 `00000` 那 100,000 成本**没有**计入
2. 存在任何失败持仓时 `partial` 为 **`true`**
3. **全部持仓都失败时**：`cost_total`、`market_value`、`profit` 均为 `0.0`，
   `profit_rate` 为 **`null`**（没有成功项可算比率），`partial` 为 `true`。
   实测形状：

   ```json
   {"cost_total": 0.0, "market_value": 0.0, "profit": 0.0,
    "profit_rate": null, "partial": true}
   ```

   > 消费方务必区分「合计为 0」与「无数据」：判据是 `profit_rate === null`
   > 且 `data[]` 中没有 `error === null` 的项。不要把 `profit: 0.0` 当成"不赚不亏"。

> `partial: true` 意味着你看到的合计**不是全部持仓**。发布前请自行决定：
> 是补齐失败项后再发，还是明确标注"部分持仓数据缺失"。

---

### 4.4 `GET /v1/calendar` — 交易日历

只读透传东证交易日历。用于判断某天是否开市。

| 参数 | 类型 | 约束 |
|---|---|---|
| `from` | date | 必填，`YYYY-MM-DD` |
| `to` | date | 必填，区间最长 400 天 |

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8642/v1/calendar?from=2026-08-06&to=2026-08-11"
```

真实响应：

```json
{
  "meta": { "...": "见第 3 节" },
  "data": [
    {"date": "2026-08-06", "holiday_division": "1", "is_trading_day": true,  "not_covered": false},
    {"date": "2026-08-07", "holiday_division": "0", "is_trading_day": false, "not_covered": false},
    {"date": "2026-08-08", "holiday_division": "0", "is_trading_day": false, "not_covered": false},
    {"date": "2026-08-10", "holiday_division": "1", "is_trading_day": true,  "not_covered": false},
    {"date": "2026-08-11", "holiday_division": "1", "is_trading_day": true,  "not_covered": false}
  ],
  "coverage_from": "2026-07-10",
  "coverage_to": "2026-09-20"
}
```

| 字段 | 说明 |
|---|---|
| `holiday_division` | 数据源 `HolDiv` 原值，**未加工透传**：`0`=非营业日 `1`=营业日 `2`=东证半日立会日 `3`=非营业日（仅大阪取引所祝日交易） |
| `is_trading_day` | 派生值。`1`/`2` → `true`；`0`/`3` → `false`；**未知取值 → `null`** |
| `not_covered` | `true` 表示该日期超出数据源覆盖范围。**服务不作猜测** |
| `coverage_from/to` | 数据源实际覆盖的日期范围，通常是当前日期前后约两个月 |

**三条注意**：

1. **`is_trading_day: null` 表示未知**，不表示非交易日。见到 `null` 应视为"无法判断"。
2. **`not_covered: true` 的日期没有任何信息**——不要把它当成非交易日。
   查询范围请落在 `coverage_from` 与 `coverage_to` 之间。
3. **`HolDiv=3` 映射为非交易日**：那天大阪取引所有衍生品交易，但**东证现货不开市**。
   本服务只关心东证现货。

日历不可用时，所有日期返回 `not_covered: true` 且 `coverage_*` 为 `null`，
**HTTP 仍是 200**——这是降级，不是错误。

---

## 5. 错误目录

所有错误都是这个形状（**成功响应有 `meta`，错误响应只有 `error`**）：

```json
{
  "error": {
    "code": "机器可读错误码",
    "message": "人类可读说明",
    "hint": "你该怎么办"
  }
}
```

### 5.1 HTTP 层错误（整个请求失败）

| 状态码 | `code` | 触发条件 | 处置 |
|---|---|---|---|
| **401** | `UNAUTHORIZED` | 缺少或错误的 `X-API-Key` | 检查 `.env` 的 `API_KEY_LOCAL`。**不要重试** |
| **400** | `INVALID_REQUEST` | 请求体不合法：`cost_price` 与 `cost_total` 双给/双缺、类型错误、`positions` 为空或超 100 | 对照第 4.3 节参数表修正。**不要重试** |
| **400** | `TOO_MANY_CODES` | `codes` 超过 100 个 | 分批调用 |
| **500** | `INTERNAL_ERROR` | 服务内部错误 | 记下**发生时刻（JST）与完整请求内容**，据此查服务日志。注意错误响应**不含 `request_id`**，只能靠时刻对齐日志 |

服务日志位置：`~/Library/Logs/pnlapi.out.log` 与 `pnlapi.err.log`（日志已做密钥脱敏）。

真实的 401：

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "缺少或错误的 X-API-Key 请求头",
    "hint": "检查 .env 中的 API_KEY_LOCAL 是否与请求头一致"
  }
}
```

真实的 400（双缺成本参数）：

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "请求不合法（positions.0: Value error, cost_price 与 cost_total 必须且只能提供一个）",
    "hint": "cost_price 与 cost_total 必须且只能给一个；对照 docs/consumer-api.md 的参数表检查"
  }
}
```

### 5.2 持仓级错误（HTTP 200，单项失败不影响其他）

出现在 `data[].error` 里，其余业务字段为 `null`。

| `code` | 含义 | 处置 |
|---|---|---|
| `UNKNOWN_CODE` | 代码不在上市证券主数据中 | 确认代码；已退市证券也会命中 |
| `NO_RECENT_PRICE` | 代码存在但回看窗口（约 45 天）内无可用收盘价 | 通常是长期停牌。若 `hint` 写「主数据不可用，代码有效性未验证」，则代码是否存在**未经确认**，稍后重试 |
| `INVALID_SHARES` | `shares` 是整数但不是正数（如 `0`、`-5`） | 传当前持有股数，整数且 > 0 |
| `INVALID_COST` | 成本是数值但非正数（如 `0`、`-100`） | 成本必须为正数 |

### 5.3 400 与 200 的切分线

**类型不对 → 400（整体拒绝）；类型对但值非法 → 200（单项错误）。**

| 你传的 | 结果 |
|---|---|
| `"shares": "abc"` | **400** `INVALID_REQUEST`——不是整数，请求体不合法 |
| `"shares": -5` | **200**，该项 `INVALID_SHARES`，其余持仓照常计算 |
| `"shares": 3.5` | **400**——不是整数 |
| `"cost_price": "x"` | **400**——不是数值 |
| `"cost_price": -100` | **200**，该项 `INVALID_COST` |
| 双给或双缺成本字段 | **400**——请求体结构不合法 |

判断依据：**请求体能不能被解析成合法结构**。不能 → 400；能解析但业务上不成立 → 200 单项错误。

---

## 6. 就绪自查协议（必读）

**服务不会替你判断数据是不是"今天的"。你必须自己核对 `as_of`。**

### 6.1 先分清三种情况

`as_of` 不等于今天，**未必是问题**。先判断今天是不是交易日：

**服务已经在 `meta` 里告诉你今天是不是交易日**（`is_trading_day_today`），
不需要你自己算日历。

```text
as_of == meta.today_jst ?
  是 → 当日数据，可作为"今日盈亏"发布
  否 ↓
     meta.is_trading_day_today == false ?
       是 → 今天不开市（周末或节假日）。as_of 就是最近一个交易日。
            可以发布，但【必须】标注"截至 <as_of> 收盘"
       否 → 今天开市（或日历不可用），当日收盘价尚未发布
            等待 10 分钟后重试，直至 as_of == today_jst
```

**两条硬性要求**：

1. **禁止把旧 `as_of` 的数字当作"今日盈亏"发布。**
2. **发布非当日数据时，`as_of` 标注是强制项**——正文里必须出现
   "截至 YYYY-MM-DD 收盘"字样，不得省略、不得只写在角落。
   读者必须一眼看到这不是今天的数字。

### 6.2 参考实现

```python
from datetime import datetime, time, timedelta, timezone

JST = timezone(timedelta(hours=9))
PUBLISH_TIME = time(16, 30)          # 当日收盘价约此时发布

def now_jst() -> datetime:
    return datetime.now(JST)

def fetch_pnl(positions: list[dict]) -> dict:
    r = httpx.post(f"{BASE_URL}/v1/pnl", headers=HEADERS, timeout=60,
                   json={"positions": positions})
    r.raise_for_status()
    return r.json()

def check_freshness(body: dict) -> tuple[str, str, str | None]:
    """返回 (状态, 说明, as_of)。状态：TODAY / STALE_OK / WAIT / NO_DATA

    本函数用「as_of 是否为今天」判断，不自己算交易日历——
    本服务不提供交易日历端点，最近交易日以 as_of 为准。
    """
    ok_rows = [row for row in body["data"] if row["error"] is None]
    if not ok_rows:
        return "NO_DATA", "没有任何成功持仓", None

    as_of = max(row["as_of"] for row in ok_rows)
    meta = body["meta"]
    today = meta["today_jst"]                    # 用服务端 JST 日期，不用本地
    is_trading = meta["is_trading_day_today"]    # true / false / null
    now = now_jst()

    if as_of == today:
        return "TODAY", f"当日数据（{as_of}）", as_of

    # 服务端日历已明确今天不开市 —— 无需猜测
    if is_trading is False:
        return "STALE_OK", f"今天非交易日，最近交易日为 {as_of}", as_of

    # 今天是交易日（或日历不可用），看是否已过发布时刻
    if now.time() < PUBLISH_TIME:
        return "WAIT", f"当日收盘价尚未发布（现在 {now:%H:%M} JST）", as_of

    if is_trading is None and now.weekday() >= 5:
        # 日历不可用时的兜底：按星期几粗判
        return "STALE_OK", f"今天是周末（日历不可用，粗判），最近交易日为 {as_of}", as_of

    return "WAIT", f"已过发布时刻但仍是 {as_of}，10 分钟后重试", as_of


body = fetch_pnl(my_positions)
status, detail, as_of = check_freshness(body)

if status == "TODAY":
    publish(body, label="今日盈亏")
elif status == "STALE_OK":
    # as_of 标注是强制项，不得省略
    publish(body, label=f"截至 {as_of} 收盘")
elif status == "WAIT":
    print(f"[wait] {detail}")           # 10 分钟后重试
else:
    print(f"[skip] {detail}")           # 无数据可发

if body["totals"]["partial"]:
    print("[warn] 部分持仓失败，合计不完整")
```

> **周末与节假日不会让脚本中止**——`STALE_OK` 是可发布状态，
> 只是**必须**带 `as_of` 标注。这与第 1 节的表格一致。

### 6.3 什么时候开始轮询

**建议起始时刻：16:35 JST。**

| 时刻 | 依据 |
|---|---|
| 官方称日线约 **16:30 JST** 发布 | J-Quants 文档 `/ja/spec/data-update` |
| 实测 **16:19 JST 已可得** | 2026-08-24（周一）实测；这是"已可得"的下界，不是首次可得的精确时刻 |
| **建议 16:35 起轮** | 在官方 16:30 基础上留 5 分钟余量 |

> 实测比官方文档更早，但**不要据此把轮询提前到 16:20**——
> 那只是一次观测，供应商的发布时刻可能因当日负载波动。
> 16:35 起轮、每 10 分钟重试，是稳妥的做法。

如果你的任务是**定时**跑（而非轮询），直接设在 **17:00 JST 之后**最省事：
那时数据必然已发布，一次调用即可拿到当日数据，也顺带避开了 6.3 的缓存问题。

---

### 6.4 缓存行为（对你透明，无需干预）

服务对行情做了两档缓存，**你不需要为它做任何事**：

| 条目状态 | 缓存策略 | 对你的意义 |
|---|---|---|
| `as_of` **是今天** | 钉到 JST 日切 | EOD 数据当天不再变，重复调用不浪费上游配额 |
| `as_of` **早于今天** | 最多 **600 秒** | 收盘价发布后，你的下一次重试**最迟 10 分钟内**就能拿到新价 |

所以 **6.1 的「等待后重试」是有效的**：即使你在 16:30 JST 之前调用过，
之后的重试仍会取到当日数据，无需任何特殊操作。

> 建议轮询间隔 **≥ 10 分钟**，与旧数据缓存周期对齐；更密集的轮询不会更快拿到数据。

---

## 7. 两条免责与责任边界

### 7.1 拆股口径的责任在你

系统假设你传入的是**当前股数**与**摊薄后成本**（也就是券商 App 里显示的口径）。

举例：你原持有 100 股、成本 4,000 円/股，该股 1:25 拆股后，券商 App 会显示
2,500 股、成本 160 円/股。**你应该传 2,500 股 + 160 円**。

若你按除权前的旧口径传参（100 股 + 4,000 円），算出的盈亏是错的——
**这是输入方的责任**，系统无法察觉。

> 系统内部：估值用**原始价**（市场真实成交价 × 你的当前股数），
> 当日涨跌用**复权价**相除（跨除权日才不会算出假涨跌）。两者不混用。

### 7.2 盈亏是毛值，不含费税

`profit` 与 `profit_rate` **不扣除**买卖手续费、交易税、股息税。
`meta.fees_included` 恒为 `false` 就是这个意思。

如果你要发布"实际到手收益"，需要自行扣减。

---

## 8. 延迟与超时（实测数据）

服务向上游串行取数并遵守限流，**请求耗时与持仓只数线性相关**。
下面是本机实测值（2026-08-08，Light 档）：

| 场景 | 实测耗时 | 构成 |
|---|---|---|
| 冷启动，单只 | **4,903 ms** | 一次性主数据（4,444 只证券）+ 一次性日历 + 1 次行情 |
| 热路径，单只（缓存命中） | **24 ms** | 无上游请求 |
| 已热，新增 1 只冷代码 | **1,317 ms** | 1 次行情请求（限流最小间隔 1.25 s 主导） |

### 什么时候是"冷"的

服务重启后的**第一次请求**要额外拉取主数据与交易日历（各约 1.5–2 s），
这两项拉一次就钉到 JST 日切，当天不再重复。

行情按代码分别缓存：`as_of` 已是当日 → 钉到日切；否则 600 秒 TTL。

### 预计耗时公式

设 `N` = 本次请求中**未命中缓存**的代码数：

```
耗时 ≈ 冷启动开销 + N × 1.3 秒

冷启动开销 = 3.5 秒（服务重启后首次请求）或 0（当天已请求过）
```

举例（服务已热）：

| 未命中代码数 | 预计耗时 |
|---|---|
| 5 | 约 7 秒 |
| 20 | 约 26 秒 |
| 50 | 约 65 秒 |
| 100（上限） | 约 130 秒 |

### 建议客户端超时值

```python
# 按最坏情况给足余量：冷启动 + 全部代码未命中
timeout = 10 + len(positions) * 2      # 秒

r = httpx.post(f"{BASE_URL}/v1/pnl", headers=HEADERS,
               json={"positions": positions}, timeout=timeout)
```

| 持仓数 | 建议 timeout |
|---|---|
| ≤ 5 | **30 s** |
| ≤ 20 | **60 s** |
| ≤ 50 | **120 s** |
| ≤ 100 | **240 s** |

> **不要用默认超时**：httpx 默认 5 秒，连冷启动的单只请求都不够。
>
> **不要靠缩短超时来"快速失败"**：中途超时会浪费已经花掉的上游配额，
> 而重试又要从头开始。宁可给足时间。
>
> **同一天的第二次调用会快得多**——当日数据钉到日切，热路径只要几十毫秒。

---

## 9. 限制速查

| 项 | 值 |
|---|---|
| `codes` 上限 | 100 个 |
| `positions` 上限 | 100 条 |
| 回看窗口 | 约 45 个自然日（≈30 个交易日） |
| 数值单位 | 价格与金额为**日元**；`pct_change_today` 与 `profit_rate` 为**小数**（非百分数） |
| 并发 | 无需考虑，单消费方设计 |
| 持仓存储 | **系统不落任何持仓数据**，持仓由你保管 |

交互式文档：`http://127.0.0.1:8642/docs`
