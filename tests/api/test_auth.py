"""Accounts, sessions, roles, CSRF, admin panel API and daily-use endpoints."""

import uuid

import pytest

from fanvaaz.auth import COOKIE, hash_password, verify_password

from ..conftest import ADMIN


def new_email() -> str:
    return f"analyst-{uuid.uuid4().hex[:8]}@example.com"


def signup(c, email=None, password="correct-horse-42", name="Asha"):
    return c.post(
        "/api/v1/auth/signup", json={"name": name, "email": email or new_email(), "password": password}
    )


def test_password_hashing_roundtrip():
    h = hash_password("correct-horse-42")
    assert h.startswith("scrypt$") and "correct-horse-42" not in h
    assert verify_password("correct-horse-42", h) and not verify_password("wrong", h)
    assert hash_password("same") != hash_password("same")  # salted


def test_signup_logs_in_with_secure_cookie(anon):
    r = signup(anon)
    assert r.status_code == 201 and r.json()["role"] == "analyst"
    cookie = r.headers["set-cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert anon.get("/api/v1/auth/me").json()["email"] == r.json()["email"]


@pytest.mark.parametrize(
    "payload, status",
    [
        # schema rules (FastAPI's list-of-errors format, documented in OpenAPI)
        ({"name": "A", "email": "not-an-email", "password": "correct-horse-42"}, 422),
        ({"name": "A", "email": "a@b.co", "password": "short"}, 422),
        ({"name": " ", "email": "a2@b.co", "password": "correct-horse-42"}, 422),
        ({"name": "", "email": "a3@b.co", "password": "correct-horse-42"}, 422),
        # business rules the schema cannot express (plain message)
        ({"name": "A", "email": "a4@b.co", "password": "password123"}, 400),
    ],
)
def test_signup_validation(anon, payload, status):
    r = anon.post("/api/v1/auth/signup", json=payload)
    assert r.status_code == status
    assert isinstance(r.json()["detail"], str if status == 400 else list)  # matches the documented schema


def test_duplicate_signup_is_409(anon):
    email = new_email()
    assert signup(anon, email).status_code == 201
    r = signup(anon, email)
    assert r.status_code == 409 and "Sign in instead" in r.json()["detail"]


def test_signup_domain_allow_list(anon, monkeypatch):
    from fanvaaz import api

    acc = api._accounts[next(iter(api._accounts))]
    monkeypatch.setattr(acc.s, "signup_domains", "atomberg.com")
    r = signup(anon, "someone@gmail.com")
    assert r.status_code == 403 and "@atomberg.com" in r.json()["detail"]
    assert signup(anon, f"x{uuid.uuid4().hex[:6]}@atomberg.com").status_code == 201


def test_login_logout_flow(anon):
    email = new_email()
    signup(anon, email)
    anon.post("/api/v1/auth/logout")
    assert anon.get("/api/v1/auth/me").status_code == 401
    r = anon.post("/api/v1/auth/login", json={"email": email.upper(), "password": "correct-horse-42"})
    assert r.status_code == 200  # email is case-insensitive
    assert anon.get("/api/v1/sov").status_code == 200
    assert anon.post("/api/v1/auth/logout").status_code == 204
    assert anon.get("/api/v1/sov").status_code == 401  # session revoked server-side


def test_wrong_password_and_unknown_email_look_the_same(anon):
    email = new_email()
    signup(anon, email)
    a = anon.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password-1"})
    b = anon.post("/api/v1/auth/login", json={"email": new_email(), "password": "wrong-password-1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_brute_force_is_throttled(anon):
    email = new_email()
    signup(anon, email)
    for _ in range(5):
        anon.post("/api/v1/auth/login", json={"email": email, "password": "wrong-password-1"})
    r = anon.post("/api/v1/auth/login", json={"email": email, "password": "correct-horse-42"})
    assert r.status_code == 429


def test_csrf_header_required_for_cookie_writes(anon):
    signup(anon)
    r = anon.post(
        "/api/v1/ask", json={"question": "Why do people like Atomberg?"}, headers={"X-Requested-With": ""}
    )
    assert r.status_code == 403
    anon.headers.pop("X-Requested-With")
    assert anon.post("/api/v1/auth/login", json={"email": "a@b.co", "password": "x"}).status_code == 403


def test_every_data_endpoint_needs_auth(anon):
    for path in [
        "/api/v1/sov",
        "/api/v1/keywords",
        "/api/v1/insights",
        "/api/v1/risks",
        "/api/v1/brief",
        "/api/v1/export/sov.csv",
        "/api/v1/admin/users",
        "/api/v1/auth/me",
    ]:
        assert anon.get(path).status_code == 401, path
    assert anon.get("/health").status_code == 200


def test_pages_redirect_when_signed_out(anon):
    for path, target in [("/app", "/login?next=/app"), ("/admin", "/login?next=/admin")]:
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == target
    assert anon.get("/").status_code == 200 and anon.get("/login").status_code == 200


def test_analyst_cannot_use_admin(anon):
    signup(anon)
    assert anon.get("/api/v1/admin/users").status_code == 403
    assert anon.post("/api/v1/runs", json={"source": "demo"}).status_code == 403
    assert anon.get("/admin", follow_redirects=False).headers["location"] == "/app"


def test_seeded_admin_can_manage_users(anon):
    target = signup(anon).json()
    anon.post("/api/v1/auth/logout")
    assert anon.post("/api/v1/auth/login", json=ADMIN).json()["role"] == "admin"
    users = anon.get("/api/v1/admin/users").json()
    assert any(u["email"] == ADMIN["email"] for u in users)
    r = anon.patch(f"/api/v1/admin/users/{target['id']}", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    r = anon.patch(f"/api/v1/admin/users/{target['id']}", json={"active": False})
    assert r.json()["active"] is False
    r = anon.patch(f"/api/v1/admin/users/{target['id']}", json={"password": "brand-new-pass-9"})
    assert r.status_code == 200
    assert anon.patch("/api/v1/admin/users/999999", json={"role": "analyst"}).status_code == 404


def test_admin_cannot_lock_themselves_out(anon):
    me = anon.post("/api/v1/auth/login", json=ADMIN).json()
    r = anon.patch(f"/api/v1/admin/users/{me['id']}", json={"role": "analyst"})
    assert r.status_code == 409


def test_deactivated_user_loses_session(anon, client):
    email = new_email()
    user = signup(anon, email).json()
    assert anon.get("/api/v1/auth/me").status_code == 200
    client.patch(f"/api/v1/admin/users/{user['id']}", json={"active": False})
    assert anon.get("/api/v1/auth/me").status_code == 401
    assert (
        anon.post("/api/v1/auth/login", json={"email": email, "password": "correct-horse-42"}).status_code
        == 401
    )


def test_admin_runs_and_system(client):
    runs = client.get("/api/v1/admin/runs").json()
    assert runs and runs[0]["source"] == "synthetic-demo"
    sysinfo = client.get("/api/v1/admin/system").json()
    assert sysinfo["youtube_key_set"] is False and sysinfo["daily_quota"] == 10_000


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


# ---- daily use: brief, risk inbox -----------------------------------------
def test_brief_compares_with_previous_run(client):
    first = client.get("/api/v1/brief").json()
    assert first["has_data"] and first["sentence"].startswith("Atomberg holds")
    assert client.post("/api/v1/runs", json={"source": "demo"}).status_code == 201
    second = client.get("/api/v1/brief").json()
    assert second["previous_run_id"] == first["run"]["run_id"]
    assert second["sov_delta"] == pytest.approx(0.0, abs=1e-9)  # same demo data -> flat
    assert "flat since the last run" in second["sentence"]
    assert second["new_high_risks"] == 0  # nothing new since the previous run


def test_risk_triage_persists_and_filters(client):
    open_flags = client.get(
        "/api/v1/risks", params={"brand": "Atomberg", "status": "open", "limit": 3}
    ).json()["flags"]
    f = open_flags[0]
    r = client.patch(
        "/api/v1/risks/status",
        json={
            "comment_id": f["comment_id"],
            "brand": f["brand"],
            "status": "handled",
            "note": "Replied on YouTube",
        },
    )
    assert r.status_code == 200 and r.json()["updated_by"] == "service@fanvaaz.local"
    handled = client.get("/api/v1/risks", params={"brand": "Atomberg", "status": "handled"}).json()["flags"]
    assert any(h["comment_id"] == f["comment_id"] for h in handled)
    still_open = client.get(
        "/api/v1/risks", params={"brand": "Atomberg", "status": "open", "limit": 1000}
    ).json()
    assert all(x["comment_id"] != f["comment_id"] for x in still_open["flags"])
    before = client.get("/api/v1/brief").json()["open_risks"]
    client.patch(
        "/api/v1/risks/status", json={"comment_id": f["comment_id"], "brand": f["brand"], "status": "open"}
    )
    assert client.get("/api/v1/brief").json()["open_risks"] == before + 1


def test_risk_status_validation(client):
    r = client.patch(
        "/api/v1/risks/status", json={"comment_id": "x", "brand": "Atomberg", "status": "deleted"}
    )
    assert r.status_code == 422


@pytest.mark.parametrize("user_id", [-1, 2**63])
def test_user_id_out_of_range_is_422_not_500(client, user_id):
    """Found by Schemathesis: 2**63 overflowed SQLite and returned 500."""
    assert client.patch(f"/api/v1/admin/users/{user_id}", json={"role": "analyst"}).status_code == 422


def test_control_characters_in_email_rejected_by_schema(anon):
    """Found by Schemathesis: Python's \\s and JSON Schema's \\s disagree on \\x1f."""
    r = anon.post(
        "/api/v1/auth/signup", json={"name": "A", "email": "0@\x1f.AA", "password": "correct-horse-42"}
    )
    assert r.status_code == 422


@pytest.mark.parametrize("body", [{"active": 0}, {"active": "false"}, {"role": "admin", "extra": 1}])
def test_admin_patch_is_strict(client, body):
    """Found by Schemathesis: "active": 0 was coerced to false and deactivated an admin."""
    users = client.get("/api/v1/admin/users").json()
    r = client.patch(f"/api/v1/admin/users/{users[0]['id']}", json=body)
    assert r.status_code == 422
    assert client.get("/api/v1/admin/users").json()[0]["active"] == users[0]["active"]


def test_service_account_id_zero_is_a_clean_404(client):
    assert client.patch("/api/v1/admin/users/0", json={"role": "analyst"}).status_code == 404


def test_password_reset_works_for_accounts_made_under_older_rules(client, seeded_store):
    """Found by Schemathesis: a reset re-validated the stored email and failed with a misleading message."""
    with seeded_store._conn() as c:
        c.execute(
            "INSERT INTO users (email,name,password_hash,role,active,created_at) VALUES (?,?,?,?,1,?)",
            ("legacy\u00fc@b.co", "Legacy", hash_password("old-password-1"), "analyst", "2026-01-01"),
        )
        uid = c.execute("SELECT id FROM users WHERE email=?", ("legacy\u00fc@b.co",)).fetchone()[0]
    assert (
        client.patch(f"/api/v1/admin/users/{uid}", json={"password": "a-fresh-password-9"}).status_code == 200
    )
