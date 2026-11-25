.PHONY: install demo live serve test unit golden safety e2e api-contract fuzz promptfoo load llm-evals lint two-pager

install:            ## dev install
	pip install -e ".[dev]" && python -m playwright install chromium

demo:               ## seed a run from the bundled synthetic dataset
	hawaaz run --source demo

live:               ## collect real YouTube data (needs HAWAAZ_YOUTUBE_API_KEY)
	hawaaz run --source youtube

serve:
	hawaaz serve --port 8000

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

api-contract:       ## Postman collection via Newman against a running server
	npx newman run postman/Hawaaz.postman_collection.json -e postman/local.postman_environment.json --env-var apiToken=$$HAWAAZ_API_TOKEN

fuzz:               ## Schemathesis against a running server
	schemathesis run http://127.0.0.1:8000/openapi.json --checks all --max-examples 40 --exclude-path /api/v1/runs \
	  -H "Authorization: Bearer $$HAWAAZ_API_TOKEN" -H "X-Requested-With: hawaaz"

promptfoo:          ## behavioural + refusal checks against a running server
	npx promptfoo@latest eval -c promptfoo/promptfooconfig.yaml

load:               ## k6 load test against a running server
	k6 run load/k6_sov.js

llm-evals:          ## DeepEval + Ragas (needs HAWAAZ_LLM_* env)
	pip install -e ".[llm-evals]" && pytest -m llm

lint:
	ruff check . && ruff format --check .

two-pager:          ## write docs/TWO_PAGER.md from the latest run
	python scripts/two_pager.py
