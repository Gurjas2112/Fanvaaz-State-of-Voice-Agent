"""API contract tests (FastAPI TestClient). Postman/Newman runs the same contract
against a live server in CI; Schemathesis fuzzes it from the OpenAPI spec."""

import pytest


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["has_data"] is True and body["llm_configured"] is False
    assert "X-Response-Time-ms" in r.headers


def test_keywords_start_with_all(client):
    ks = client.get("/api/v1/keywords").json()
    assert ks[0] == "all" and "smart fan" in ks and len(ks) == 7


@pytest.mark.parametrize("kw", ["all", "smart fan", "BLDC fan"])
def test_sov_schema(client, kw):
    r = client.get("/api/v1/sov", params={"keyword": kw})
    assert r.status_code == 200
    body = r.json()
    assert body["focus_brand"] == "Atomberg"
    assert body["run"]["source"] == "synthetic-demo"
    brands = body["report"]["brands"]
    assert brands, "expected at least one brand"
    assert [b["sov"] for b in brands] == sorted((b["sov"] for b in brands), reverse=True)
    for b in brands:
        assert set(b) >= {
            "brand",
            "sov",
            "mention_share",
            "engagement_share",
            "positive_share",
            "net_sentiment",
        }


def test_unknown_keyword_is_404(client):
    r = client.get("/api/v1/sov", params={"keyword": "air cooler"})
    assert r.status_code == 404
    assert "Unknown keyword" in r.json()["detail"]


def test_insights_have_evidence(client):
    data = client.get("/api/v1/insights").json()
    assert len(data["insights"]) >= 5
    for ins in data["insights"]:
        assert {"id", "kind", "title", "text", "evidence"} <= set(ins)


def test_ask_happy_path(client):
    r = client.post("/api/v1/ask", json={"question": "Why do people complain about Atomberg's remote?"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is False and body["mode"] == "extractive"
    assert "Atomberg" in body["answer"] and body["sources"]


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 1001}, {"q": "hi"}])
def test_ask_validation(client, payload):
    assert client.post("/api/v1/ask", json=payload).status_code == 422


def test_runs_reject_wrong_token(client):
    r = client.post("/api/v1/runs", json={"source": "demo"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_runs_youtube_without_key_is_400(client):
    r = client.post("/api/v1/runs", json={"source": "youtube"})
    assert r.status_code == 400 and "YOUTUBE_API_KEY" in r.json()["detail"]


def test_openapi_lists_every_route(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in [
        "/health",
        "/api/v1/sov",
        "/api/v1/keywords",
        "/api/v1/insights",
        "/api/v1/ask",
        "/api/v1/runs",
    ]:
        assert p in paths


def test_pages_served(client):
    for path in ["/", "/app", "/admin"]:
        r = client.get(path)
        assert r.status_code == 200 and "Hawaaz" in r.text, path
    assert client.get("/static/app.js").status_code == 200


def test_risks_endpoint_filters(client):
    body = client.get("/api/v1/risks", params={"brand": "Atomberg", "severity": "high", "limit": 5}).json()
    assert body["categories"]["safety"] == 50
    assert 0 < len(body["flags"]) <= 5
    assert all(f["brand"] == "Atomberg" and f["severity"] == "high" for f in body["flags"])
    assert [f["score"] for f in body["flags"]] == sorted((f["score"] for f in body["flags"]), reverse=True)
    assert body["summary"]["Atomberg"]["total"] >= body["summary"]["Atomberg"]["high"]


@pytest.mark.parametrize("params", [{"severity": "critical"}, {"limit": 0}, {"limit": 5000}])
def test_risks_validation(client, params):
    assert client.get("/api/v1/risks", params=params).status_code == 422


@pytest.mark.parametrize(
    "table, header",
    [
        ("sov", "run_id,keyword,brand,sov"),
        ("risks", "run_id,brand,severity,score"),
        ("mentions", "run_id,keyword,video_id,source"),
    ],
)
def test_csv_exports(client, table, header):
    r = client.get(f"/api/v1/export/{table}.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith(header) and "attachment" in r.headers["content-disposition"]
    assert len(r.text.splitlines()) > 10


def test_unknown_export_is_422(client):
    assert client.get("/api/v1/export/users.csv").status_code == 422
