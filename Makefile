PY  := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.venv:
	python3 -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"

.PHONY: install
install: .venv  ## set up the venv

.PHONY: test
test: install  ## run all 286 tests (~40s)
	$(PY) -m pytest

.PHONY: test-fast
test-fast: install  ## skip the slow ones (the real 3s deadline, subprocesses)
	$(PY) -m pytest -m "not slow"

.PHONY: lint
lint: install  ## ruff
	.venv/bin/ruff check src tests scripts

.PHONY: demo
demo: install  ## every task end to end, narrated
	$(PY) scripts/demo.py

.PHONY: bench
bench: install  ## what the guardrail costs, over real sockets
	$(PY) scripts/bench.py

.PHONY: inspect
inspect: install  ## task 1 in the MCP Inspector (needs npx)
	npx -y @modelcontextprotocol/inspector $(PY) -m tollgate.mcp_server

.PHONY: run-mcp
run-mcp: install  ## task 1 on stdio
	$(PY) -m tollgate.mcp_server

.PHONY: run-gateway
run-gateway: install  ## task 2: billing server :8801 behind the gateway :8800
	$(PY) -m tollgate.mcp_server --http 8801 --admin-tools & \
	TOLLGATE_DOWNSTREAM_URL=http://127.0.0.1:8801/mcp $(PY) -m tollgate.mcp_gateway --port 8800

.PHONY: run-llm
run-llm: install  ## tasks 3+4: mock providers :8831/:8832 behind the LLM gateway :8830
	$(PY) -m tollgate.mock_llm --name primary --port 8831 & \
	$(PY) -m tollgate.mock_llm --name secondary --port 8832 & \
	$(PY) -m tollgate.llm_gateway --port 8830

.PHONY: token
token: install  ## print a dev JWT, e.g. make token ROLE=admin
	@.venv/bin/tollgate token --role $(or $(ROLE),viewer)

.PHONY: up
up:  ## everything in docker compose
	docker compose up --build

.PHONY: clean
clean:  ## remove the venv, caches and the rate limit database
	rm -rf .venv .pytest_cache .ruff_cache .hypothesis src/*.egg-info data
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
