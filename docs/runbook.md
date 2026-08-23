# 运维手册

面向运维者（Jack），不是消费方。消费方文档见 `docs/consumer-api.md`。

---

## 服务启停

服务由 launchd 常驻托管（`com.jack.pnlapi`，KeepAlive）。

```bash
# 状态
launchctl print gui/$(id -u)/com.jack.pnlapi | grep -E "state =|pid =|runs ="

# 重启（先杀后拉，约 5 秒恢复）
launchctl kickstart -k gui/$(id -u)/com.jack.pnlapi

# 停用 / 启用
launchctl bootout   gui/$(id -u)/com.jack.pnlapi
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jack.pnlapi.plist
```

日志：`~/Library/Logs/pnlapi.out.log` 与 `pnlapi.err.log`（已做密钥脱敏）。

---

## 应急：强制清空缓存

**正常情况下不需要这么做。** 缓存有两档自动失效策略
（当日数据钉到 JST 日切、旧数据 600 秒 TTL），消费方的等待-重试协议自会生效。

仅在以下情况考虑手动清缓存：

- 怀疑缓存中存有错误数据（例如上游曾返回异常值）
- 改了 `CACHE_STALE_TTL_S` 需要立即生效

缓存是纯进程内的，**重启服务即清空**：

```bash
launchctl kickstart -k gui/$(id -u)/com.jack.pnlapi
sleep 6 && curl -s -H "X-API-Key: $(grep '^API_KEY_LOCAL=' ~/projects/pnl-api/.env | cut -d= -f2-)" \
  http://127.0.0.1:8642/v1/health
```

> **此手段不对消费方开放**：`consumer-api.md` 不提供清缓存指引，
> 消费方一律按「等待-重试」协议处理，不应依赖运维干预。

---

## 配置项

改 `.env` 后需重启服务生效。

| 键 | 默认 | 说明 |
|---|---|---|
| `JQUANTS_API_KEY` | — | 必填，永不入日志 |
| `API_BIND` | `127.0.0.1` | 非此值拒绝启动 |
| `API_PORT` | `8642` | |
| `API_KEY_LOCAL` | — | 必填，消费方鉴权 |
| `LOOKBACK_DAYS` | `45` | 取价回看自然日窗口 |
| `TIMEOUT_S` | `10` | 单次上游请求超时 |
| `CACHE_STALE_TTL_S` | `600` | 旧数据缓存秒数；当日数据不受此限 |

---

## 上游限流

Light 档官方限流 60 请求/分。客户端固定 0.8 rps（80% 余量）串行请求，
正常使用不会触发 429。若日志出现 429 退避记录，检查是否有异常轮询。
