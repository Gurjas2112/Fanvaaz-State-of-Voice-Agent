.PHONY: install live replay snapshot serve mcp graphs test unit golden safety e2e pg-test harness api-contract fuzz promptfoo load llm-evals lint report capture apk deploy

install:            ## dev install
	pip install -e ".[dev]" && python -m playwright install chromium

SNAPSHOT ?= tests/fixtures/youtube_snapshot/smartphones-india.json

live:               ## collect live YouTube data now (needs FANVAAZ_YOUTUBE_API_KEY)
	fanvaaz run --source youtube

replay:             ## re-score the recorded real collection (no key, no network)
	fanvaaz run --source replay --snapshot $(SNAPSHOT)

snapshot:           ## record a fresh real snapshot for the offline tests (~500 quota units)
	python scripts/record_snapshot.py --profile smartphones-india --top-n 25 --comments 50

serve:
	fanvaaz serve --port 8000

mcp:                ## MCP server over stdio (Claude Desktop / Claude Code)
	fanvaaz mcp

graphs:             ## print both LangGraph agents as Mermaid
	fanvaaz graphs

test:               ## every offline gate (unit, metamorphic, golden, API, safety)
	pytest -m "not e2e and not mobile and not llm"

unit:
	pytest tests/unit
golden:
	pytest -m golden
safety:
	pytest -m safety
e2e:                ## Playwright (starts its own server)
	pytest -m e2e

pg-test:            ## whole suite on PostgreSQL 16 + pgvector in Docker (container is auto-removed on stop)
	docker run --rm -d --name fanvaaz-pgtest -e POSTGRES_PASSWORD=fanvaaz -e POSTGRES_DB=fanvaaz -p 55432:5432 pgvector/pgvector:pg16
	sleep 6
	FANVAAZ_TEST_DATABASE_URL=postgresql://postgres:fanvaaz@127.0.0.1:55432/fanvaaz pytest -m "not e2e and not mobile and not llm"; \
	  status=$$?; docker stop fanvaaz-pgtest; exit $$status

harness:            ## every offline gate + e2e, reports written to reports/
	pytest -m "not mobile and not llm" --junitxml=reports/junit.xml

api-contract:       ## Postman collection via Newman against a running server
	npx newman run postman/Fanvaaz.postman_collection.json -e postman/local.postman_environment.json --env-var apiToken=$$FANVAAZ_API_TOKEN

fuzz:               ## Schemathesis against a running server (changes state: run it last)
	schemathesis run http://127.0.0.1:8000/openapi.json --checks all --max-examples 40 --exclude-path /api/v1/runs \
	  -H "Authorization: Bearer $$FANVAAZ_API_TOKEN" -H "X-Requested-With: fanvaaz"

promptfoo:          ## behavioural + refusal checks against a running server
	npx promptfoo@latest eval -c promptfoo/promptfooconfig.yaml

load:               ## k6 load test against a running server
	k6 run load/k6_sov.js

llm-evals:          ## DeepEval + Ragas (needs FANVAAZ_LLM_* env)
	pip install -e ".[llm-evals]" && pytest -m llm

lint:
	ruff check . && ruff format --check .

report:             ## write docs/RUN_REPORT.md from the latest run
	python scripts/run_report.py

capture:            ## flow screenshots + walkthrough video against APP_URL (default: local)
	python scripts/capture_flows.py --url $${APP_URL:-http://127.0.0.1:8000}

apk:                ## Android app (Capacitor shell for the live site at APP_URL)
	cd mobile && npm install && npx cap sync android && cd android && ./gradlew assembleRelease

deploy:             ## Cloud Run + Cloud SQL (interactive: asks for secrets)
	bash deploy/gcp_cloud_run.sh
