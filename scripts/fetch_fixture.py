"""抓取真实 J-Quants V2 响应作为 fixture（提示词第 6 节，检查点 1 的输入）。

伪造 fixture 视同项目失败。本脚本只取原样响应，不做任何业务解析——
解析与数学代码在检查点 1（Jack 确认字段映射与 NO_TRADE 判定）之后才允许编写。

采样清单（第 6 节 4 项 + 审计追加 1 项，见 decisions.md）：
  1. 正常股票的日线区间响应（7203 丰田，回看 45 天）
  2. 当日无成交/停牌样本：取最近交易日全市场，从中找 O/C 为 null 的行
  3. "当日数据尚未发布"的形态：请求今天(JST)的日线
     ——注意运行时刻：交易日 15:00 JST 前运行本脚本才是提示词要的场景；
       非交易日运行得到的是"非交易日"形态，两者须在文档中分开标注
  4. 主数据（名称）响应
  5. 乱码/不存在代码的响应形态（UNKNOWN_CODE 判定依赖，审计追加）

用法：uv run python scripts/fetch_fixture.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx

from pnlapi.clock import now_jst, today_jst
from pnlapi.config import get_settings

BASE = "https://api.jquants.com"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# Light 档官方限流 60 请求/分（实测出处：jp-market 项目 docs/field_inventory.md）。
# 取 80% -> 0.8 rps -> 请求间隔 1.25s。
REQUEST_INTERVAL_S = 1.25


def fetch(client: httpx.Client, key: str, path: str, params: dict) -> httpx.Response:
    time.sleep(REQUEST_INTERVAL_S)
    return client.get(path, params=params, headers={"x-api-key": key})


def save(name: str, response: httpx.Response, params: dict, note: str, key: str,
         trim: int | None = None) -> dict:
    """原样保存（可裁剪行数，字段完整）。落盘前断言不含 key。"""
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"_non_json_body": response.text[:2000]}
    kept = body
    total_rows = len(body.get("data", [])) if isinstance(body.get("data"), list) else None
    if trim and isinstance(body.get("data"), list) and len(body["data"]) > trim:
        kept = {k: (v[:trim] if k == "data" else v) for k, v in body.items()}

    text = json.dumps(kept, ensure_ascii=False, indent=1)
    if key in text:
        raise RuntimeError(f"fixture {name} 中出现 API key，拒绝落盘")
    (FIXTURE_DIR / f"{name}.json").write_text(text + "\n", encoding="utf-8")

    meta = {
        "endpoint": params.pop("_path"),
        "params": params,  # 无鉴权信息
        "http_status": response.status_code,
        "fetched_at_jst": now_jst().isoformat(),
        "total_rows": total_rows,
        "rows_kept": len(kept.get("data", [])) if isinstance(kept.get("data"), list) else None,
        "note": note,
    }
    (FIXTURE_DIR / f"{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"  {name}: HTTP {response.status_code}, {total_rows} 行")
    return body


def main() -> int:
    settings = get_settings()
    key = settings.jquants_api_key.get_secret_value().strip()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    today = today_jst()
    start = today - timedelta(days=settings.lookback_days)

    with httpx.Client(base_url=BASE, timeout=settings.timeout_s) as client:
        print(f"[1] 正常股票日线区间（7203，{start}..{today}）")
        r = fetch(client, key, "/v2/equities/bars/daily",
                  {"code": "7203", "from": start.isoformat(), "to": today.isoformat()})
        body = save("bars_normal_7203", r,
                    {"_path": "/v2/equities/bars/daily", "code": "7203",
                     "from": start.isoformat(), "to": today.isoformat()},
                    "正常股票；最后一行的日期即'最新可得收盘'的实测样本", key)
        latest_date = (body.get("data") or [{}])[-1].get("Date")
        print(f"    最新可得日期: {latest_date}（脚本运行于 {now_jst().isoformat()}）")

        print("[2] 无成交/停牌样本（最近可得交易日全市场，从中筛 null 行）")
        probe_day = latest_date or (today - timedelta(days=3)).isoformat()
        r = fetch(client, key, "/v2/equities/bars/daily", {"date": probe_day})
        try:
            rows = r.json().get("data", [])
        except json.JSONDecodeError:
            rows = []
        null_rows = [row for row in rows if row.get("O") is None][:5]
        normal_rows = [row for row in rows if row.get("O") is not None][:2]
        if null_rows:
            payload = {"data": normal_rows + null_rows,
                       "_trim_note": f"自 {probe_day} 全市场 {len(rows)} 行中筛出"
                                     f" {len(null_rows)} 个无成交样本 + 2 个正常行对照"}
            text = json.dumps(payload, ensure_ascii=False, indent=1)
            assert key not in text
            (FIXTURE_DIR / "bars_no_trade_sample.json").write_text(text + "\n", "utf-8")
            print(f"    找到无成交行 {len(null_rows)} 个（全市场共 "
                  f"{sum(1 for x in rows if x.get('O') is None)} 个）")
        else:
            (FIXTURE_DIR / "bars_no_trade_sample.json").write_text(
                json.dumps({"data": [], "_note": f"{probe_day} 全市场无 O=null 行，未找到"},
                           ensure_ascii=False) + "\n", "utf-8")
            print("    未找到（如实记录）")

        print(f"[3] 请求今天({today})的日线——'尚未发布/非交易日'形态")
        r = fetch(client, key, "/v2/equities/bars/daily",
                  {"code": "7203", "date": today.isoformat()})
        save("bars_today_probe", r,
             {"_path": "/v2/equities/bars/daily", "code": "7203",
              "date": today.isoformat()},
             f"运行时刻 {now_jst().isoformat()}；今天是否交易日、是否已过 16:30 JST "
             "决定本样本代表'未发布'还是'非交易日'，判读见 fixture-notes", key)

        print("[4] 主数据（名称来源）")
        r = fetch(client, key, "/v2/equities/master", {})
        save("master_names", r, {"_path": "/v2/equities/master"},
             "code->name 映射来源；裁剪保留 30 行", key, trim=30)

        print("[5] 不存在的代码（UNKNOWN_CODE 判定依据）")
        r = fetch(client, key, "/v2/equities/bars/daily",
                  {"code": "00000", "from": start.isoformat(), "to": today.isoformat()})
        save("bars_unknown_code", r,
             {"_path": "/v2/equities/bars/daily", "code": "00000",
              "from": start.isoformat(), "to": today.isoformat()},
             "乱码代码的响应形态", key)

    print(f"\n完成。fixture 目录: {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
