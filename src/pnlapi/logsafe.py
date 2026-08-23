"""日志脱敏（提示词第 4 节：HTTP 日志与异常堆栈落盘前过滤 x-api-key）。"""

from __future__ import annotations

import logging
import re
import sys

from pnlapi.config import secret_values

REDACTED = "***REDACTED***"

# x-api-key: xxx / "x-api-key": "xxx" / x_api_key=xxx
_HEADER_RE = re.compile(
    r"""(?ix)(['"]?x[-_]api[-_]key['"]?\s*[:=]\s*)(['"]?)([^\s'",}]+)(['"]?)"""
)


def redact(text: str) -> str:
    if not text:
        return text
    for secret in secret_values():
        if secret in text:
            text = text.replace(secret, REDACTED)
    return _HEADER_RE.sub(rf"\1\2{REDACTED}\4", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # 先完成 %-格式化再脱敏：分开处理会把 msg 里的 %s 当密钥抹掉，
            # 占位符与 args 数量对不上导致 Formatter 崩溃。
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info:
                record.exc_text = redact(
                    logging.Formatter().formatException(record.exc_info)
                )
                record.exc_info = None
        except Exception:  # noqa: BLE001 - 脱敏失败绝不搞挂日志
            record.msg = "[脱敏失败，本条日志内容已丢弃]"
            record.args = None
            record.exc_info = None
            record.exc_text = None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """幂等。压制 httpx/httpcore 的 INFO（其请求行含完整 URL）。"""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if getattr(handler, "_pnlapi", False):
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S%z")
    )
    handler.addFilter(RedactingFilter())
    handler._pnlapi = True  # type: ignore[attr-defined]
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
