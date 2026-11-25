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
        assert r.status_code == 200 and "Fanvaaz" in r.text, path
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


# ---- tracking profile ---------------------------------------------------------
def test_profile_is_readable_by_any_signed_in_user(client):
    r = client.get("/api/v1/profile")
    assert r.status_code == 200
    assert r.json()["focus_brand"] == "Atomberg"
    assert "Havells" in r.json()["rivals"]


def test_admin_can_switch_profile_and_past_runs_keep_their_brands(client):
    before = client.get("/api/v1/sov").json()
    try:
        r = client.put("/api/v1/admin/profile", json={"preset": "ather-scooters"})
        assert r.status_code == 200, r.text
        assert r.json()["active"]["focus_brand"] == "Ather"
        assert client.get("/api/v1/profile").json()["focus_brand"] == "Ather"
        # The stored run still reports on the brand it tracked.
        after = client.get("/api/v1/sov").json()
        assert after["focus_brand"] == "Atomberg"
        assert after["report"] == before["report"]
        # No synthetic scooter data ships, so a demo run explains what to do instead.
        r = client.post("/api/v1/runs", json={"source": "demo"})
        assert r.status_code == 400
        assert "collect live YouTube data" in r.json()["detail"]
    finally:
        assert client.put("/api/v1/admin/profile", json={"preset": "atomberg-fans"}).status_code == 200


def test_custom_profile_is_validated_with_a_readable_error(client):
    bad = {
        "id": "acme",
        "name": "Acme",
        "category": "kettles",
        "focus_brand": "Nobody",
        "brands": {"Acme": ["acme"], "Boilex": ["boilex"]},
        "keywords": ["kettle"],
    }
    r = client.put("/api/v1/admin/profile", json={"profile": bad})
    assert r.status_code == 422
    assert "must be one of the tracked brands" in r.json()["detail"][0]["msg"]
    ok = {**bad, "focus_brand": "Acme"}
    r = client.put("/api/v1/admin/profile", json={"preset": "atomberg-fans", "profile": ok})
    assert r.status_code == 422 and "not both" in r.json()["detail"][0]["msg"]
    assert client.put("/api/v1/admin/profile", json={}).status_code == 422
    assert client.put("/api/v1/admin/profile", json={"preset": "nope"}).status_code == 422
    for demo in ("../../etc/passwd", "a" * 5000, "atomberg-fans.json/.."):
        r = client.put("/api/v1/admin/profile", json={"profile": {**ok, "demo_data": demo}})
        assert r.status_code == 422, demo  # found by Schemathesis: a 5,000-char name was a 500
    assert client.get("/api/v1/profile").json()["focus_brand"] == "Atomberg"


def test_profile_admin_endpoints_need_an_admin(anon):
    assert anon.get("/api/v1/admin/profile").status_code == 401
    assert anon.put("/api/v1/admin/profile", json={"preset": "ather-scooters"}).status_code == 401


def test_405_lists_every_method_the_path_supports(client):
    """Found by Schemathesis (ST205): GET and PUT are separate routes on one path."""
    r = client.delete("/api/v1/admin/profile")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET, PUT"
