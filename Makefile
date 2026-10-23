.PHONY: help install test test-live lint fmt build clean

help:
	@echo "install  创建 venv 并安装（可编辑）"
	@echo "test     跑测试（不需要网络）"
	@echo "test-live 对真实主机跑并发验收（LSSH_TEST_SSH=bj）"
	@echo "lint     ruff check + format --check"
	@echo "fmt      自动格式化"
	@echo "build    构建 sdist/wheel"

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

test:
	.venv/bin/python -m pytest -q

test-live:
	LSSH_TEST_SSH=$${HOST:-bj} .venv/bin/python -m pytest -q tests/test_bj_live.py -v

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

fmt:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

build:
	.venv/bin/python -m build

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
