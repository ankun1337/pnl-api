"""基础模块测试：clock / config / cache / logsafe。

供应商解析与盈亏数学的测试（T1–T6）在检查点 1（fixture 字段确认）之后才编写。
T7（日志无 key）的核心断言在此先行落地。
"""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta, timezone

import pytest

from pnlapi import cache as cache_module
from pnlapi import logsafe
from pnlapi.clock import JST, now_jst, today_jst
from pnlapi.config import Settings, _parse_env_file
from pnlapi.logsafe import REDACTED, RedactingFilter, redact

FAKE_KEY = "jq_live_0123456789abcdef0123456789abcdef"


# --- clock -----------------------------------------------------------------


def test_jst_is_fixed_utc_plus_9():
    assert JST.utcoffset(None) == timedelta(hours=9)
    assert now_jst().utcoffset() == timedelta(hours=9)
    assert isinstance(today_jst(), date)


# --- config ----------------------------------------------------------------


def test_parse_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# 注释\nJQUANTS_API_KEY=abc123\n\nAPI_PORT=9000\nBROKEN_LINE\n"
        "SPACED = with space \n",
        encoding="utf-8",
    )
    parsed = _parse_env_file(env)
    assert parsed == {
        "JQUANTS_API_KEY": "abc123",
        "API_PORT": "9000",
        "SPACED": "with space",
    }
    assert _parse_env_file(tmp_path / "missing.env") == {}


def test_settings_rejects_non_loopback():
    with pytest.raises(ValueError, match="127.0.0.1"):
        Settings(
            jquants_api_key="k", api_key_local="l", api_bind="0.0.0.0"
        )


def test_settings_defaults():
    s = Settings(jquants_api_key="supersecret-jq-key", api_key_local="supersecret-local")
    assert s.api_bind == "127.0.0.1"
    assert s.api_port == 8642
    assert s.lookback_days == 45
    assert s.timeout_s == 10
    dumped = repr(s)
    assert "supersecret-jq-key" not in dumped  # SecretStr 掩码
    assert "supersecret-local" not in dumped
    assert s.jquants_api_key.get_secret_value() == "supersecret-jq-key"


# --- cache（严格按提示词：JST 日期翻转即失效）------------------------------


def test_cache_hit_same_day():
    c = cache_module.DayCache()
    c.put("7203", {"close": 3132}, as_of=today_jst())
    assert c.get("7203") == {"close": 3132}
    assert len(c) == 1
    assert c.get("9432") is None


def test_cache_expires_on_jst_date_rollover(monkeypatch):
    c = cache_module.DayCache()
    c.put("7203", "old", as_of=today_jst())
    # 模拟日期翻转
    tomorrow = today_jst() + timedelta(days=1)
    monkeypatch.setattr(cache_module, "today_jst", lambda: tomorrow)
    assert c.get("7203") is None
    assert len(c) == 0


def test_cache_clear():
    c = cache_module.DayCache()
    c.put("a", 1)
    c.clear()
    assert c.get("a") is None


# --- 两档失效策略（检查点 2 批复第 1 条）----------------------------------


class FakeClock:
    """可控单调时钟，精确验证 TTL 边界。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_stale_entry_expires_after_ttl():
    """旧数据（as_of < 今天）到 TTL 后必须重新取价。

    这是就绪协议能工作的前提：收盘价发布后，消费方重试能拿到新价。
    """
    clock = FakeClock()
    c = cache_module.DayCache(stale_ttl_s=600, monotonic=clock)
    yesterday = today_jst() - timedelta(days=1)

    c.put("7203", "昨日行情", as_of=yesterday)
    assert c.get("7203") == "昨日行情"          # 刚写入，命中

    clock.advance(599)
    assert c.get("7203") == "昨日行情"          # TTL 内，仍命中（挡掉重复轮询）

    clock.advance(2)                            # 累计 601 秒
    assert c.get("7203") is None                # 过期，将重新取价
    assert len(c) == 0


def test_today_entry_pinned_until_date_rollover():
    """当日数据钉到 JST 日切，TTL 对它无效——EOD 当天不再变。"""
    clock = FakeClock()
    c = cache_module.DayCache(stale_ttl_s=600, monotonic=clock)

    c.put("7203", "当日行情", as_of=today_jst())
    clock.advance(10_000)                       # 远超 TTL
    assert c.get("7203") == "当日行情"          # 仍钉住

    clock.advance(50_000)
    assert c.get("7203") == "当日行情"


def test_stale_then_refreshed_to_today_becomes_pinned():
    """旧数据过期→重取到当日数据后，转为钉住模式（完整就绪流程）。"""
    clock = FakeClock()
    c = cache_module.DayCache(stale_ttl_s=600, monotonic=clock)
    yesterday = today_jst() - timedelta(days=1)

    c.put("7203", "发布前", as_of=yesterday)
    clock.advance(601)
    assert c.get("7203") is None                # 过期，触发重取

    c.put("7203", "发布后", as_of=today_jst())  # 取到当日数据
    clock.advance(10_000)
    assert c.get("7203") == "发布后"            # 此后钉住，不再重复请求


def test_as_of_none_treated_as_pinned():
    """主数据无 as_of 概念，按当日数据处理（钉到日切）。"""
    clock = FakeClock()
    c = cache_module.DayCache(stale_ttl_s=600, monotonic=clock)
    c.put("master", {"7203": "トヨタ"})
    clock.advance(10_000)
    assert c.get("master") == {"7203": "トヨタ"}


def test_ttl_zero_means_stale_never_cached():
    """CACHE_STALE_TTL_S=0 时旧数据不缓存（每次都重取）。"""
    clock = FakeClock()
    c = cache_module.DayCache(stale_ttl_s=0, monotonic=clock)
    c.put("7203", "旧", as_of=today_jst() - timedelta(days=1))
    assert c.get("7203") is None


# --- logsafe（T7 的核心断言）-----------------------------------------------


@pytest.fixture()
def captured(monkeypatch):
    monkeypatch.setattr(logsafe, "secret_values", lambda: [FAKE_KEY])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    root.handlers, root.level = [handler], logging.DEBUG
    yield stream
    root.handlers, root.level = old_handlers, old_level


def test_redact_exact_and_header_forms(monkeypatch):
    monkeypatch.setattr(logsafe, "secret_values", lambda: [FAKE_KEY])
    assert FAKE_KEY not in redact(f"泄露 {FAKE_KEY} 了")
    monkeypatch.setattr(logsafe, "secret_values", lambda: [])
    for form in (f"x-api-key: {FAKE_KEY}", f'"X-API-Key": "{FAKE_KEY}"'):
        out = redact(form)
        assert FAKE_KEY not in out and REDACTED in out


def test_key_never_reaches_log_output(captured):
    logging.getLogger("t").info("请求头 x-api-key: %s", FAKE_KEY)
    logging.getLogger("t").error("裸打印 %s", FAKE_KEY)
    try:
        raise RuntimeError(f"堆栈里带 key {FAKE_KEY}")
    except RuntimeError:
        logging.getLogger("t").exception("失败")
    output = captured.getvalue()
    assert FAKE_KEY not in output
    assert REDACTED in output
