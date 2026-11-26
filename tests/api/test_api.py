"""API contract tests (FastAPI TestClient). Postman/Newman runs the same contract
against a live server in CI; Schemathesis fuzzes it from the OpenAPI spec."""

import json

import pytest


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["has_data"] is True and body["llm_configured"] is False
    assert body["profile_configured"] is True
    assert "X-Response-Time-ms" in r.headers


def test_ready_reports_database_and_dependencies(client):
    r = client.get("/ready")
    assert r.status_code == 200
    checks = r.json()["checks"]
    assert checks["database"] is True and checks["schema_version"] >= 3
    assert checks["youtube_key"] is False and checks["llm"].startswith("off")


def test_keywords_start_with_all(client):
    ks = client.get("/api/v1/keywords").json()
    assert ks[0] == "all" and "best camera phone India" in ks and len(ks) == 5


@pytest.mark.parametrize("kw", ["all", "best camera phone India", "smartphone battery test"])
def test_sov_schema(client, kw):
    r = client.get("/api/v1/sov", params={"keyword": kw})
    assert r.status_code == 200
    body = r.json()
    assert body["focus_brand"] == "OnePlus"
    assert body["run"]["source"] == "replay"  # a recorded real YouTube collection
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
        assert {"id", "kind", "title", "text", "evidence", "citations", "verified"} <= set(ins)
        assert ins["verified"] is True, ins  # grounded numbers and resolvable citations
        assert ins["citations"], ins["id"]


def test_evidence_pack(client):
    run_id = client.get("/api/v1/runs/latest").json()["run_id"]
    r = client.get(f"/api/v1/runs/{run_id}/evidence")
    assert r.status_code == 200
    pack = r.json()
    assert pack["verified_share"] == 1.0 and "SoV =" in pack["method"]["share_of_voice"]
    cite = pack["insights"][0]["citations"][0]
    assert cite["url"].startswith("https://www.youtube.com/watch?v=")
    assert client.get("/api/v1/runs/20990101T000000-abcdef/evidence").status_code == 404
    assert client.get("/api/v1/runs/not-a-run/evidence").status_code == 422


def test_ask_happy_path(client):
    r = client.post("/api/v1/ask", json={"question": "What do people say about the Samsung camera?"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is False and body["mode"] == "extractive"
    assert "Samsung" in body["answer"] and body["sources"]
    assert body["scope"]["aspect"] == "camera" and body["citations"][0] == "c1"
    assert any(step.startswith("plan:") for step in body["reasoning_steps"])
    assert body["context"]["used_tokens"] <= body["context"]["budget_tokens"]
    assert body["thread_id"] and body["trace_id"]


def test_ask_follow_up_uses_thread_memory(client):
    first = client.post("/api/v1/ask", json={"question": "How is Samsung on battery?"}).json()
    follow = client.post(
        "/api/v1/ask", json={"question": "and camera?", "thread_id": first["thread_id"]}
    ).json()
    assert follow["scope"]["brands"] == ["Samsung"] and follow["scope"]["aspect"] == "camera"
    assert any("follow-up" in s for s in follow["reasoning_steps"])
    assert client.delete(f"/api/v1/ask/threads/{first['thread_id']}").status_code == 204


def test_ask_repeat_is_served_from_the_verified_cache(client):
    client.post("/api/v1/ask", json={"question": "What do viewers say about Vivo display?"})
    again = client.post("/api/v1/ask", json={"question": "what do viewers say about the Vivo display"}).json()
    assert again["cache"]["hit"] is True and again["cache"]["similarity"] >= 0.8


def test_ask_is_rate_limited_per_user(client, monkeypatch):
    from fanvaaz import api
    from fanvaaz.config import get_settings

    monkeypatch.setattr(get_settings(), "ask_rate_per_min", 1)
    api._ask_hits.clear()
    codes = [client.post("/api/v1/ask", json={"question": "Samsung battery?"}).status_code for _ in range(2)]
    assert codes == [200, 429]
    api._ask_hits.clear()


def test_prefs_round_trip(client):
    r = client.put("/api/v1/me/prefs", json={"brands": ["Samsung", "NotABrand"], "aspects": ["camera"]})
    assert r.status_code == 200 and r.json()["brands"] == ["Samsung"]
    assert client.get("/api/v1/me/prefs").json()["aspects"] == ["camera"]
    client.put("/api/v1/me/prefs", json={"brands": [], "aspects": []})


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 1001}, {"q": "hi"}])
def test_ask_validation(client, payload):
    assert client.post("/api/v1/ask", json=payload).status_code == 422


def test_runs_reject_wrong_token(client):
    r = client.post("/api/v1/runs", json={"source": "youtube"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_demo_source_no_longer_exists(client):
    assert client.post("/api/v1/runs", json={"source": "demo"}).status_code == 422


def test_runs_youtube_without_key_is_400(client):
    r = client.post("/api/v1/runs", json={"source": "youtube"})
    assert r.status_code == 400 and "YOUTUBE_API_KEY" in r.json()["detail"]
    r = client.post("/api/v1/runs/jobs", json={})
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
        "/api/v1/runs/jobs",
        "/api/v1/runs/{run_id}/evidence",
        "/api/v1/admin/ai-ops",
        "/ready",
        "/metrics",
    ]:
        assert p in paths


def test_ai_ops_audit_metrics_and_graphs(client):
    ops = client.get("/api/v1/admin/ai-ops").json()
    assert {"calls", "tokens", "cost_usd", "by_node", "spans", "governance", "prompts"} <= set(ops)
    assert any(k.startswith("collect.") for k in ops["spans"])
    assert {p["id"] for p in ops["prompts"]} >= {"sentiment", "rewrite", "ask", "plan"}
    assert ops["governance"]["model_allowed"] is True
    rag = ops["rag"]  # offline seed has no embedder: chunks indexed, none embedded
    assert rag["run_id"] and rag["chunks"] > 1000 and 0 <= rag["embedded"] <= rag["chunks"]
    actions = {e["action"] for e in client.get("/api/v1/admin/audit?limit=1000").json()}
    assert {"run.start", "run.done"} <= actions
    m = client.get("/metrics")
    assert m.status_code == 200 and "fanvaaz_llm_calls_total" in m.text
    g = client.get("/api/v1/admin/graphs").json()
    assert "relabel" in g["collect"] and "verify" in g["ask"]


def test_ops_endpoints_need_admin(anon):
    for path in ("/metrics", "/api/v1/admin/ai-ops", "/api/v1/admin/audit"):
        assert anon.get(path).status_code == 401


def test_pages_served(client):
    for path in ["/", "/app", "/admin"]:
        r = client.get(path)
        assert r.status_code == 200 and "Fanvaaz" in r.text, path
    assert client.get("/static/app.js").status_code == 200


def test_risks_endpoint_filters(client):
    body = client.get("/api/v1/risks", params={"brand": "OnePlus", "severity": "high", "limit": 5}).json()
    assert body["categories"]["safety"] == 50
    assert 0 < len(body["flags"]) <= 5
    assert all(f["brand"] == "OnePlus" and f["severity"] == "high" for f in body["flags"])
    assert [f["score"] for f in body["flags"]] == sorted((f["score"] for f in body["flags"]), reverse=True)
    assert body["summary"]["OnePlus"]["total"] >= body["summary"]["OnePlus"]["high"]


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
    assert r.content.startswith(bytes([0xEF, 0xBB, 0xBF]))  # BOM: Excel reads Hindi and emoji correctly
    assert r.text.lstrip(chr(0xFEFF)).startswith(header) and "attachment" in r.headers["content-disposition"]
    assert len(r.text.splitlines()) > 10


def test_excel_workbook_export(client, anon):
    import io

    from openpyxl import load_workbook

    r = client.get("/api/v1/export/report.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert r.headers["content-disposition"].endswith('.xlsx"')
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Summary", "Share of voice", "Insights", "Reply inbox", "Mentions"]
    sov = wb["Share of voice"]
    assert [c.value for c in sov[1]][:4] == ["run_id", "keyword", "brand", "sov"]
    assert isinstance(sov["D2"].value, float)  # numbers stay numbers
    assert wb["Mentions"].max_row > 100
    # no cell may become a formula, whatever a comment says
    assert not any(
        isinstance(c.value, str) and c.value.startswith("=")
        for ws in wb
        for row in ws.iter_rows()
        for c in row
    )
    assert anon.get("/api/v1/export/report.xlsx").status_code == 401


def test_unknown_export_is_422(client):
    assert client.get("/api/v1/export/users.csv").status_code == 422


# ---- tracking profile ---------------------------------------------------------
def test_profile_is_readable_by_any_signed_in_user(client):
    r = client.get("/api/v1/profile")
    assert r.status_code == 200
    assert r.json()["focus_brand"] == "OnePlus"
    assert "Samsung" in r.json()["rivals"]


def test_admin_can_switch_profile_and_past_runs_keep_their_brands(client):
    before = client.get("/api/v1/sov").json()
    try:
        r = client.put("/api/v1/admin/profile", json={"preset": "ev-scooters-india"})
        assert r.status_code == 200, r.text
        assert r.json()["active"]["focus_brand"] == "Ather"
        assert client.get("/api/v1/profile").json()["focus_brand"] == "Ather"
        # The new product has no collection yet; the smartphone run is still there under its own profile.
        assert client.get("/api/v1/sov").status_code == 404
        phones = client.get("/api/v1/sov?profile=smartphones-india").json()
        assert phones["focus_brand"] == "OnePlus" and phones["report"] == before["report"]
        audit = client.get("/api/v1/admin/audit").json()
        assert audit[0]["action"] == "profile.set" and audit[0]["target"] == "ev-scooters-india"
    finally:
        assert client.put("/api/v1/admin/profile", json={"preset": "smartphones-india"}).status_code == 200
        client.delete("/api/v1/admin/profiles/ev-scooters-india")


def test_two_products_side_by_side(client, anon):
    """Add a second product without replacing the first; requests are scoped per product."""
    try:
        r = client.put("/api/v1/admin/profile", json={"preset": "ev-scooters-india", "make_default": False})
        assert r.status_code == 200
        ids = {p["id"]: p for p in r.json()["profiles"]}
        assert ids["smartphones-india"]["is_default"] and not ids["ev-scooters-india"]["is_default"]
        assert ids["smartphones-india"]["last_run_at"] and ids["ev-scooters-india"]["last_run_at"] is None
        # the default is unchanged: a request with no choice still sees smartphones
        client.cookies.clear()
        assert client.get("/api/v1/profile").json()["id"] == "smartphones-india"
        assert client.get("/api/v1/sov").status_code == 200
        # the switcher's cookie and ?profile= both scope a request
        assert client.get("/api/v1/profile?profile=ev-scooters-india").json()["focus_brand"] == "Ather"
        client.cookies.set("fz_profile", "ev-scooters-india")
        assert client.get("/api/v1/profile").json()["focus_brand"] == "Ather"
        assert client.get("/api/v1/brief").json() == {"has_data": False}
        # an unknown or malformed id falls back to the default instead of failing
        assert client.get("/api/v1/profile?profile=nope").json()["id"] == "smartphones-india"
        assert client.get("/api/v1/profile?profile=../x").json()["id"] == "smartphones-india"
        # the default cannot be deleted; others can, and their history stays
        assert client.delete("/api/v1/admin/profiles/smartphones-india").status_code == 409
        assert client.post("/api/v1/admin/profiles/nope/default").status_code == 404
        assert anon.get("/api/v1/profiles").status_code == 401
    finally:
        client.cookies.clear()
        assert client.delete("/api/v1/admin/profiles/ev-scooters-india").status_code in (204, 404)
    assert [p["id"] for p in client.get("/api/v1/profiles").json()] == ["smartphones-india"]


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
    r = client.put("/api/v1/admin/profile", json={"preset": "smartphones-india", "profile": ok})
    assert r.status_code == 422 and "not both" in r.json()["detail"][0]["msg"]
    assert client.put("/api/v1/admin/profile", json={}).status_code == 422
    assert client.put("/api/v1/admin/profile", json={"preset": "nope"}).status_code == 422
    for name in ("a" * 5000, "x"):
        r = client.put("/api/v1/admin/profile", json={"profile": {**ok, "name": name}})
        assert r.status_code == 422, name  # found by Schemathesis: a 5,000-char name was a 500
    r = client.put("/api/v1/admin/profile", json={"profile": {**ok, "demo_data": "x.json"}})
    assert r.status_code == 422  # unknown fields are rejected
    assert client.get("/api/v1/profile").json()["focus_brand"] == "OnePlus"


def test_profile_admin_endpoints_need_an_admin(anon):
    assert anon.get("/api/v1/admin/profile").status_code == 401
    assert anon.put("/api/v1/admin/profile", json={"preset": "ev-scooters-india"}).status_code == 401


def test_405_lists_every_method_the_path_supports(client):
    """Found by Schemathesis (ST205): GET and PUT are separate routes on one path."""
    r = client.delete("/api/v1/admin/profile")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET, PUT"


def test_resume_endpoint(client, anon, latest):
    run_id = latest["info"].run_id
    r = client.post(f"/api/v1/runs/{run_id}/resume")  # already finished: returned unchanged
    assert r.status_code == 200 and r.json()["run_id"] == run_id
    assert client.post("/api/v1/runs/20990101T000000-abcdef/resume").status_code == 404
    assert client.post("/api/v1/runs/not-a-run/resume").status_code == 422
    assert anon.post(f"/api/v1/runs/{run_id}/resume").status_code == 401


def test_ask_stream_sends_steps_then_result(client):
    with client.stream(
        "POST", "/api/v1/ask/stream", json={"question": "What do people say about OnePlus battery?"}
    ) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [blk for blk in body.split("\n\n") if blk.strip()]
    kinds = [e.split("\n")[0].removeprefix("event: ") for e in events]
    assert kinds[-1] == "result" and "step" in kinds
    result = json.loads(events[-1].split("\n", 1)[1].removeprefix("data: "))
    assert result["route"] == "data" and result["thread_id"] and result["follow_ups"]


def test_ask_routes_help_and_regenerate_skips_cache(client):
    help_ = client.post("/api/v1/ask", json={"question": "How is share of voice calculated?"}).json()
    assert help_["route"] == "app" and help_["mode"] == "help"
    q = {"question": "What do people complain about with Samsung's camera?"}
    client.post("/api/v1/ask", json=q)
    again = client.post("/api/v1/ask", json={**q, "fresh": True}).json()
    assert "cache: skipped (regenerate)" in again["reasoning_steps"]


def test_ask_feedback_is_stored_and_summarised(client):
    out = client.post("/api/v1/ask", json={"question": "Why do people complain about OnePlus?"}).json()
    fb = {
        "trace_id": out["trace_id"],
        "rating": 1,
        "question": "Why…",
        "answer": out["answer"][:200],
        "mode": out["mode"],
        "route": out["route"],
    }
    assert client.post("/api/v1/ask/feedback", json=fb).status_code == 201
    assert (
        client.post(
            "/api/v1/ask/feedback", json={**fb, "rating": -1, "comment": "mail me a@b.com"}
        ).status_code
        == 201
    )
    assert client.post("/api/v1/ask/feedback", json={**fb, "rating": 5}).status_code == 422
    f = client.get("/api/v1/admin/ai-ops").json()["feedback"]
    assert f["up"] >= 1 and f["down"] >= 1 and f["by_route"]["data"]["up"] >= 1
