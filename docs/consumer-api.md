# 消费方接入文档

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
  "generated_at": "2026-08-23T16:36:23.285772+09:00",
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

---

## 4. 端点

三个端点，都需要 `X-API-Key` 头。

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

**代码写法**：四位习惯代码（`7203`）与五位供应商代码（`72030`）都可以，
响应中的 `code` 会**原样回显你传入的写法**。

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "http://127.0.0.1:8642/v1/quotes?codes=7203,6758,00000"
```

```python
r = httpx.get(f"{BASE_URL}/v1/quotes",
              params={"codes": "7203,6758"}, headers=HEADERS, timeout=60)
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
      "code": "7203",
      "name_ja": "トヨタ自動車",
      "latest_close": 3132.0,
      "as_of": "2026-08-21",
      "pct_change_today": 0.021526418786692758,
      "error": null
    },
    {
      "code": "6758",
      "name_ja": "ソニーグループ",
      "latest_close": 3785.0,
      "as_of": "2026-08-21",
      "pct_change_today": 0.0021180831347630395,
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
| `pct_change_today` | 较前一交易日的涨跌幅，**小数**（`0.0215` = +2.15%）。用复权价计算，跨除权日不会出假值 |
| `error` | 成功时为 `null`；失败时见第 5 节 |

---

### 4.3 `POST /v1/pnl` — 持仓盈亏

请求体：

```json
{
  "positions": [
    {"code": "7203", "shares": 300, "cost_price": 2800},
    {"code": "6758", "shares": 200, "cost_total": 700000}
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
  -d '{"positions":[{"code":"7203","shares":300,"cost_price":2800}]}' \
  "http://127.0.0.1:8642/v1/pnl"
```

```python
r = httpx.post(f"{BASE_URL}/v1/pnl", headers=HEADERS, timeout=60, json={
    "positions": [
        {"code": "7203", "shares": 300, "cost_price": 2800},
        {"code": "6758", "shares": 200, "cost_total": 700000},
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
    "generated_at": "2026-08-23T16:36:23.285772+09:00",
    "source": "JQUANTS_V2",
    "latency_class": "END_OF_DAY",
    "fees_included": false
  },
  "data": [
    {
      "code": "7203",
      "name_ja": "トヨタ自動車",
      "shares": 300,
      "cost_total": 840000.0,
      "latest_close": 3132.0,
      "as_of": "2026-08-21",
      "market_value": 939600.0,
      "profit": 99600.0,
      "profit_rate": 0.11857142857142858,
      "pct_change_today": 0.021526418786692758,
      "error": null
    },
    {
      "code": "6758",
      "name_ja": "ソニーグループ",
      "shares": 200,
      "cost_total": 700000.0,
      "latest_close": 3785.0,
      "as_of": "2026-08-21",
      "market_value": 757000.0,
      "profit": 57000.0,
      "profit_rate": 0.08142857142857143,
      "pct_change_today": 0.0021180831347630395,
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
    "cost_total": 1540000.0,
    "market_value": 1696600.0,
    "profit": 156600.0,
    "profit_rate": 0.10168831168831169,
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

```text
as_of == 今天(JST) ?
  是 → 当日数据，可作为"今日盈亏"发布
  否 ↓
     今天是交易日吗？（周末、日本节假日都不是）
       不是 → 正常。as_of 就是最近一个交易日。
              可以发布，但必须标注"截至 <as_of> 收盘"，
              不得称为"今日盈亏"
       是   ↓
            现在过了 16:30 JST 吗？
              没过 → 当日收盘价尚未发布，等待后重试
              过了 → 见 6.3「缓存限制」，多半是缓存钉住了
```

**禁止把旧 `as_of` 的数字当作"今日盈亏"发布。**

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

def check_freshness(body: dict) -> tuple[str, str]:
    """返回 (状态, 说明)。状态取值：TODAY / STALE_OK / WAIT / STUCK / NO_DATA

    注意：本函数用「as_of 是否为今天」判断，而不是自己算交易日历——
    本服务不提供交易日历端点，最近交易日以 as_of 为准。
    """
    ok_rows = [row for row in body["data"] if row["error"] is None]
    if not ok_rows:
        return "NO_DATA", "没有任何成功持仓"

    as_of_set = {row["as_of"] for row in ok_rows}
    as_of = max(as_of_set)
    now = now_jst()
    today = now.date().isoformat()

    if as_of == today:
        return "TODAY", f"当日数据（{as_of}）"

    # as_of 不是今天。周末与节假日属正常——今天没开市。
    if now.weekday() >= 5:
        return "STALE_OK", f"今天是周末，最近交易日为 {as_of}"
    if now.time() < PUBLISH_TIME:
        return "WAIT", f"当日收盘价尚未发布（现在 {now:%H:%M} JST），最近为 {as_of}"
    # 工作日且已过发布时刻却仍是旧日期：可能是节假日，也可能是缓存钉住
    return "STUCK", (
        f"工作日 {now:%H:%M} JST 仍返回 {as_of}。"
        "若今天是日本节假日属正常；否则见 6.3 缓存限制"
    )

body = fetch_pnl(my_positions)
status, detail = check_freshness(body)

if status == "TODAY":
    publish(body, label="今日盈亏")
elif status == "STALE_OK":
    publish(body, label=f"截至 {max(r['as_of'] for r in body['data'] if r['error'] is None)} 收盘")
elif status == "WAIT":
    print(f"[wait] {detail}")          # 稍后重试
else:
    print(f"[alert] {status}: {detail}")   # 需要人工介入，不要发布

if body["totals"]["partial"]:
    print("[warn] 部分持仓失败，合计不完整")
```

> **周末不会让脚本中止**——`STALE_OK` 是可发布状态，只是必须改标注。
> 这一点与第 1 节的表格一致。

### 6.3 ⚠️ 缓存限制：当日首次请求请放在 16:30 JST 之后

服务对每个代码的行情做了**当日缓存**（同一代码当天只向上游取一次，
缓存在 JST 日期翻转时失效）。

**后果**：如果你在当天 16:30 JST **之前**请求过某个代码，
该代码当天将**持续返回旧的 `as_of`**，即使 16:30 之后收盘价已经发布。
此时**重试没有用**——这就是上面 `STUCK` 状态的成因。

**这是已知的既定行为，不是故障**（设计取舍见项目 `docs/decisions.md`）。

**规避方法（按优先级）**：

1. **首选**：当日对任一代码的**首次**请求放在 **16:30 JST 之后**。
   把你的定时任务设在 17:00 JST 以后即可完全避开。
2. **已经踩到了**（工作日、已过 16:30、仍返回旧 `as_of`）：
   服务**没有**提供刷新参数或清缓存端点。唯一手段是**重启服务**，
   缓存随进程消失：

   ```bash
   launchctl kickstart -k gui/$(id -u)/com.jack.pnlapi
   ```

   `-k` 表示先杀后拉；服务由 launchd 常驻托管，会自动重启（约 5 秒）。
   重启后首次请求即取到最新数据。**这需要本机 shell 权限**，
   若你的消费脚本无权执行，请联系运维。

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

## 8. 限制速查

| 项 | 值 |
|---|---|
| `codes` 上限 | 100 个 |
| `positions` 上限 | 100 条 |
| 回看窗口 | 约 45 个自然日（≈30 个交易日） |
| 数值单位 | 价格与金额为**日元**；`pct_change_today` 与 `profit_rate` 为**小数**（非百分数） |
| 并发 | 无需考虑，单消费方设计 |
| 持仓存储 | **系统不落任何持仓数据**，持仓由你保管 |

交互式文档：`http://127.0.0.1:8642/docs`
