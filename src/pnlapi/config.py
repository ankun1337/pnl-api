"""配置。本模块是全仓唯一读取 .env 的地方；缺键即启动失败（提示词第 4 节）。

刻意不用 pydantic-settings——技术栈清单写明"没有别的了"（第 2 节），
.env 解析手工十行即可，校验交给 pydantic 模型。
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 开头为注释，不支持引号与转义（也不需要）。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


class Settings(BaseModel):
    """强类型配置。字段名与 .env 键一一对应（大写）。"""

    jquants_api_key: SecretStr = Field(min_length=1)
    api_bind: str = "127.0.0.1"
    api_port: int = Field(default=8642, ge=1, le=65535)
    api_key_local: SecretStr = Field(min_length=1)
    lookback_days: int = Field(default=45, ge=7, le=120)
    timeout_s: float = Field(default=10, gt=0)
    # 旧数据（as_of < 今天）的缓存存活秒数。当日数据不受此限，钉到 JST 日切。
    # 检查点 2 批复新增：修正原规格「日期翻转即失效」导致的就绪协议失效。
    cache_stale_ttl_s: float = Field(default=600, ge=0)

    @field_validator("api_bind")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        """红线：绑定非 127.0.0.1 拒绝启动。"""
        if value != "127.0.0.1":
            raise ValueError(
                f"API_BIND 只允许 127.0.0.1，收到 {value!r}——本服务不对外网提供"
            )
        return value


class ConfigError(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    raw = _parse_env_file(ENV_FILE)
    required = ("JQUANTS_API_KEY", "API_KEY_LOCAL")
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ConfigError(
            f".env 缺少必填键: {', '.join(missing)}（文件: {ENV_FILE}）。"
            "参照 .env.example 填写后重启"
        )
    return Settings(
        jquants_api_key=raw["JQUANTS_API_KEY"],
        api_bind=raw.get("API_BIND", "127.0.0.1"),
        api_port=int(raw.get("API_PORT", "8642")),
        api_key_local=raw["API_KEY_LOCAL"],
        lookback_days=int(raw.get("LOOKBACK_DAYS", "45")),
        timeout_s=float(raw.get("TIMEOUT_S", "10")),
        cache_stale_ttl_s=float(raw.get("CACHE_STALE_TTL_S", "600")),
    )


def secret_values() -> list[str]:
    """需要从日志中抹掉的明文（供 logsafe 使用）。配置未就绪时返回空。"""
    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001 - 缺配置不应搞挂日志系统
        return []
    return [
        v for v in (
            settings.jquants_api_key.get_secret_value(),
            settings.api_key_local.get_secret_value(),
        ) if v
    ]
