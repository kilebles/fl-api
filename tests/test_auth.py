import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.auth.mailer import EmailMessage, ResendMailer
from src.core.settings import Settings


def _settings(tmp_path: Path, **extra) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        work_dir=tmp_path / "work",
        storage_dir=tmp_path / "storage",
        accounts_dir=tmp_path / "accounts",
        **extra,
    )


def _mailer() -> tuple[ResendMailer, list[EmailMessage]]:
    outbox: list[EmailMessage] = []

    async def capture(msg: EmailMessage) -> None:
        outbox.append(msg)

    return ResendMailer(sender=capture), outbox


def _code_from(msg: EmailMessage) -> str:
    m = re.search(r"\b(\d{6})\b", msg.text)
    assert m is not None
    return m.group(1)


def _register_and_verify(
    client: TestClient,
    outbox: list[EmailMessage],
    *,
    username: str,
    email: str,
    password: str,
    headers: dict[str, str] | None = None,
) -> dict:
    reg = client.post(
        "/api/auth/register",
        headers=headers,
        json={"username": username, "email": email, "password": password},
    )
    assert reg.status_code == 202
    assert reg.json()["verification_required"] is True
    code = _code_from(outbox[-1])
    ver = client.post(
        "/api/auth/email/verify/confirm",
        headers=headers,
        json={"email": email, "code": code},
    )
    assert ver.status_code == 200
    return ver.json()


def test_mandatory_email_verification_username_login_sessions_and_csrf(tmp_path: Path):
    mailer, outbox = _mailer()
    app = create_app(_settings(tmp_path, frontend_url="http://localhost:3000"), mailer=mailer)

    with TestClient(app) as unverified_squatter, TestClient(app) as alice_browser, TestClient(app) as second_device:
        assert alice_browser.get("/api/projects").status_code == 401
        assert alice_browser.get("/api/auth/me").status_code == 401
        assert alice_browser.get("/api/auth/session").json() == {"user": None}

        # Validation: short password or invalid username rejected
        assert (
            alice_browser.post(
                "/api/auth/register",
                json={"username": "alice", "email": "a@b.co", "password": "short"},
            ).status_code
            == 422
        )
        assert (
            alice_browser.post(
                "/api/auth/register",
                json={"username": "абв", "email": "a@b.co", "password": "strong-password-1"},
            ).status_code
            == 422
        )

        # An unverified registration does NOT issue a session and CANNOT claim first-user admin!
        sq = unverified_squatter.post(
            "/api/auth/register",
            json={"username": "squatter", "email": "fake@example.com", "password": "fake-password-1"},
        )
        assert sq.status_code == 202
        assert unverified_squatter.get("/api/auth/session").json() == {"user": None}
        blocked_login = unverified_squatter.post(
            "/api/auth/login",
            json={"login": "squatter", "password": "fake-password-1"},
        )
        assert blocked_login.status_code == 403
        assert blocked_login.json()["code"] == "email_not_verified"
        assert blocked_login.json()["email"] == "fake@example.com"

        # Alice registers with username + email + password and confirms via 6-digit Resend code -> becomes first verified admin!
        ua_mac = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Chrome/128.0.0.0 Safari/537.36"}
        me = _register_and_verify(
            alice_browser,
            outbox,
            username="  @Alice_Dev ",
            email="  Alice@Example.com ",
            password="strong-password-1",
            headers=ua_mac,
        )
        assert me["username"] == "alice_dev"
        assert me["email"] == "alice@example.com"
        assert me["role"] == "admin"
        assert me["email_verified"] is True
        assert me["has_password"] is True and me["has_google"] is False

        # Duplicate verified email OR duplicate username rejected with 409
        assert (
            second_device.post(
                "/api/auth/register",
                json={"username": "other_nick", "email": "alice@example.com", "password": "another-password"},
            ).status_code
            == 409
        )
        assert (
            second_device.post(
                "/api/auth/register",
                json={"username": "ALICE_DEV", "email": "other@example.com", "password": "another-password"},
            ).status_code
            == 409
        )

        # Login works by username AND by email; wrong password rejected
        assert (
            second_device.post("/api/auth/login", json={"login": "alice_dev", "password": "wrong-password"}).status_code
            == 401
        )
        login_by_username = second_device.post(
            "/api/auth/login",
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile Safari/604.1"
            },
            json={"login": "ALICE_DEV", "password": "strong-password-1"},
        )
        assert login_by_username.status_code == 200 and login_by_username.json()["username"] == "alice_dev"

        # Active sessions list shows both devices
        sessions = alice_browser.get("/api/auth/sessions").json()
        assert len(sessions) == 2
        assert sessions[0]["is_current"] is True and sessions[0]["device"] == "Chrome · macOS"
        assert sessions[1]["is_current"] is False and sessions[1]["device"] == "Safari · iOS"

        # CSRF check blocks untrusted cross-origin mutations on cookie auth
        csrf = alice_browser.patch(
            "/api/auth/me",
            headers={"Origin": "https://evil.attacker.example"},
            json={"username": "hacked"},
        )
        assert csrf.status_code == 403

        # Username update & verified email change flow
        upd_u = alice_browser.patch(
            "/api/auth/me",
            headers={"Origin": "http://localhost:3000"},
            json={"username": "alice.pro"},
        )
        assert upd_u.status_code == 200 and upd_u.json()["username"] == "alice.pro"

        assert (
            alice_browser.post("/api/auth/email/change/request", json={"new_email": "alice2@example.com"}).status_code
            == 401
        )
        chg_req = alice_browser.post(
            "/api/auth/email/change/request",
            json={"new_email": "alice2@example.com", "current_password": "strong-password-1"},
        )
        assert chg_req.status_code == 200
        chg_code = _code_from(outbox[-1])
        chg_ok = alice_browser.post("/api/auth/email/change/confirm", json={"code": chg_code})
        assert chg_ok.status_code == 200 and chg_ok.json()["email"] == "alice2@example.com"

        # Changing password revokes other sessions while keeping current session alive
        pw_res = alice_browser.post(
            "/api/auth/password",
            json={"current_password": "strong-password-1", "new_password": "new-strong-password-2"},
        )
        assert pw_res.status_code == 200 and pw_res.json()["revoked_sessions"] == 1
        assert alice_browser.get("/api/auth/me").status_code == 200

        revoked_check = second_device.get("/api/auth/me")
        assert revoked_check.status_code == 401
        assert "fl_session=" in revoked_check.headers.get("set-cookie", "")


def test_multi_user_isolation_and_admin_controls(tmp_path: Path):
    mailer, outbox = _mailer()
    app = create_app(_settings(tmp_path), mailer=mailer)
    with TestClient(app) as admin_client, TestClient(app) as bob_client:
        admin = _register_and_verify(
            admin_client,
            outbox,
            username="admin_root",
            email="admin@example.com",
            password="admin-password-1",
        )
        bob = _register_and_verify(
            bob_client,
            outbox,
            username="bob_editor",
            email="bob@example.com",
            password="bob-password-123",
        )
        assert admin["role"] == "admin" and bob["role"] == "user"

        admin_proj = admin_client.post("/api/projects", json={"title": "Ролик Админа"}).json()
        bob_proj = bob_client.post("/api/projects", json={"title": "Ролик Боба"}).json()

        assert [p["title"] for p in admin_client.get("/api/projects").json()] == ["Ролик Админа"]
        assert [p["title"] for p in bob_client.get("/api/projects").json()] == ["Ролик Боба"]
        assert bob_client.patch(f"/api/projects/{admin_proj['id']}", json={"title": "Украл"}).status_code == 404
        assert bob_client.delete(f"/api/projects/{admin_proj['id']}").status_code == 404

        admin_upload = admin_client.post("/api/uploads", files={"file": ("a.png", b"png")}).json()["key"]
        assert (
            bob_client.post(
                "/api/jobs",
                json={"mode": "t2v", "project_id": admin_proj["id"], "rows": [{"name": "1", "prompt": "p"}]},
            ).status_code
            == 404
        )
        assert (
            bob_client.post(
                "/api/avatars",
                json={"name": "Чужой", "image_keys": [admin_upload]},
            ).status_code
            == 422
        )

        admin_job = admin_client.post(
            "/api/jobs",
            json={"mode": "t2v", "project_id": admin_proj["id"], "rows": [{"name": "1", "prompt": "p"}]},
        ).json()
        assert bob_client.get("/api/jobs").json() == []
        assert bob_client.get(f"/api/jobs/{admin_job['id']}").status_code == 404

        assert bob_client.get("/api/accounts").status_code == 403
        assert admin_client.get("/api/accounts").status_code == 200
        assert bob_client.get("/api/admin/users").status_code == 403
        assert len(admin_client.get("/api/admin/users").json()) == 2

        assert admin_client.patch(f"/api/admin/users/{admin['id']}", json={"is_active": False}).status_code == 422
        assert admin_client.patch(f"/api/admin/users/{bob['id']}", json={"is_active": False}).status_code == 200
        assert bob_client.get("/api/projects").status_code == 401
        assert (
            bob_client.post("/api/auth/login", json={"login": "bob_editor", "password": "bob-password-123"}).status_code
            == 403
        )


def test_google_oauth_pkce_flow_and_linking(tmp_path: Path):
    settings = _settings(
        tmp_path,
        google_client_id="gid-123",
        google_client_secret="gsec-456",
        frontend_url="http://localhost:3000",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/api/auth/providers").json() == {
            "email": True,
            "google": True,
            "password_reset": False,
        }

        async def fake_exchanger(code: str, verifier: str, redirect_uri: str, _settings: Settings):
            assert code == "auth-code-xyz" and len(verifier) >= 32
            assert redirect_uri.endswith("/api/auth/google/callback")
            return {
                "sub": "google-sub-999",
                "email": "googleuser@example.com",
                "email_verified": True,
                "name": "Google User",
                "picture": "https://lh3.googleusercontent.com/avatar.jpg",
            }

        app.state.google_exchanger = fake_exchanger

        start = client.get("/api/auth/google/start?next=/avatars", follow_redirects=False)
        assert start.status_code == 302
        qs = parse_qs(urlparse(start.headers["location"]).query)
        assert qs["code_challenge_method"] == ["S256"]

        bad_cb = client.get("/api/auth/google/callback?code=auth-code-xyz&state=tampered", follow_redirects=False)
        assert bad_cb.status_code == 302 and "auth_error=" in bad_cb.headers["location"]

        start = client.get("/api/auth/google/start?next=/avatars", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        cb = client.get(f"/api/auth/google/callback?code=auth-code-xyz&state={state}", follow_redirects=False)
        assert cb.status_code == 302
        assert cb.headers["location"] == "http://localhost:3000/avatars"

        me = client.get("/api/auth/me").json()
        assert me["email"] == "googleuser@example.com"
        assert me["username"] == "googleuser"
        assert me["email_verified"] is True
        assert me["has_google"] is True and me["has_password"] is False

        assert client.delete("/api/auth/google").status_code == 422
        assert client.post("/api/auth/password", json={"new_password": "added-local-password"}).status_code == 200
        assert client.delete("/api/auth/google").json()["has_google"] is False


def test_verification_link_and_password_reset(tmp_path: Path):
    mailer, outbox = _mailer()
    app = create_app(_settings(tmp_path, frontend_url="http://localhost:3000"), mailer=mailer)

    with TestClient(app) as reg_client, TestClient(app) as link_client:
        assert reg_client.get("/api/auth/providers").json() == {
            "email": True,
            "google": False,
            "password_reset": True,
        }

        # Register -> 1-click verification link signs user in directly
        res = reg_client.post(
            "/api/auth/register",
            json={"username": "lena_motion", "email": "lena@example.com", "password": "lena-password-1"},
        )
        assert res.status_code == 202
        ver_token = parse_qs(
            urlparse(re.search(r"http://localhost:3000/verify-email\?token=[^\s]+", outbox[-1].text).group(0)).query
        )["token"][0]

        ver_res = link_client.post("/api/auth/email/verify/confirm", json={"token": ver_token})
        assert ver_res.status_code == 200
        assert ver_res.json()["username"] == "lena_motion"
        assert link_client.get("/api/auth/me").json()["email_verified"] is True

        # Password reset: non-enumerating for unknown email, revokes existing sessions for real user
        before_count = len(outbox)
        assert reg_client.post("/api/auth/password/forgot", json={"email": "nobody@example.com"}).status_code == 200
        assert len(outbox) == before_count

        assert reg_client.post("/api/auth/password/forgot", json={"email": "lena@example.com"}).status_code == 200
        assert len(outbox) == before_count + 1
        reset_token = parse_qs(
            urlparse(re.search(r"http://localhost:3000/reset-password\?token=[^\s]+", outbox[-1].text).group(0)).query
        )["token"][0]

        reset_res = reg_client.post(
            "/api/auth/password/reset",
            json={"token": reset_token, "new_password": "lena-new-password-99"},
        )
        assert reset_res.status_code == 200
        assert reg_client.get("/api/auth/me").json()["username"] == "lena_motion"
        # Old session in link_client was revoked on password reset
        assert link_client.get("/api/auth/me").status_code == 401
        assert (
            link_client.post(
                "/api/auth/password/reset",
                json={"token": reset_token, "new_password": "another-password"},
            ).status_code
            == 401
        )


def test_gateway_forward_auth_cross_node_sync(tmp_path: Path):
    shared_secret = "shared-prod-gateway-secret-1234567890"
    gw_dir = tmp_path / "gateway"
    wk_dir = tmp_path / "worker"
    gw_dir.mkdir()
    wk_dir.mkdir()

    seed_app = create_app(_settings(wk_dir, auth_required=False))
    with TestClient(seed_app) as seed_client:
        assert seed_client.post("/api/projects", json={"title": "Legacy Worker Project"}).status_code == 201

    mailer, outbox = _mailer()
    gw_app = create_app(_settings(gw_dir, auth_secret=shared_secret, frontend_url="https://motionpr.org"), mailer=mailer)
    wk_app = create_app(_settings(wk_dir, auth_secret=shared_secret, frontend_url="https://motionpr.org"))

    with TestClient(gw_app) as admin_gw, TestClient(gw_app) as user_gw, TestClient(wk_app) as worker:
        assert admin_gw.get("/api/auth/verify").status_code == 401

        admin_me = _register_and_verify(
            admin_gw,
            outbox,
            username="elisey",
            email="admin@motionpr.org",
            password="admin-password-1",
        )
        admin_id = admin_me["id"]

        csrf_fail = admin_gw.get(
            "/api/auth/verify",
            headers={"X-Forwarded-Method": "POST", "Origin": "https://evil.example"},
        )
        assert csrf_fail.status_code == 403

        verify_admin = admin_gw.get(
            "/api/auth/verify",
            headers={"X-Forwarded-Method": "POST", "Origin": "https://motionpr.org"},
        )
        assert verify_admin.status_code == 200
        gw_hdr = verify_admin.headers["X-Fl-Gateway-Auth"]

        worker_projects = worker.get("/api/projects", headers={"X-Fl-Gateway-Auth": gw_hdr})
        assert worker_projects.status_code == 200
        assert [p["title"] for p in worker_projects.json()] == ["Legacy Worker Project"]
        wk_me = worker.get("/api/auth/me", headers={"X-Fl-Gateway-Auth": gw_hdr}).json()
        assert wk_me["id"] == admin_id and wk_me["username"] == "elisey"

        _register_and_verify(
            user_gw,
            outbox,
            username="member1",
            email="member@motionpr.org",
            password="member-password-1",
        )
        user_hdr = user_gw.get("/api/auth/verify").headers["X-Fl-Gateway-Auth"]
        assert worker.get("/api/projects", headers={"X-Fl-Gateway-Auth": user_hdr}).json() == []
        assert worker.get("/api/projects", headers={"X-Fl-Gateway-Auth": f"{gw_hdr}tampered"}).status_code == 401
