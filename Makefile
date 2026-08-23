.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help run test fixture

help:  ## 显示可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

run: _require-env  ## 启动服务（127.0.0.1:8642）
	uv run python -m pnlapi.api

test:  ## 运行测试
	uv run pytest

fixture: _require-env  ## 用真实 key 抓取 fixture（检查点 1 的输入）
	uv run python scripts/fetch_fixture.py

.PHONY: _require-env
_require-env:
	@test -f .env || { echo "缺少 .env：cp .env.example .env 并填写"; exit 1; }
