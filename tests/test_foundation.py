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
    c.put("7203", {"close": 3132})
    assert c.get("7203") == {"close": 3132}
    assert len(c) == 1
    assert c.get("9432") is None


def test_cache_expires_on_jst_date_rollover(monkeypatch):
    c = cache_module.DayCache()
    c.put("7203", "old")
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
