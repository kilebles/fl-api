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


def test_register_login_sessions_password_and_csrf(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as alice_browser, TestClient(app) as second_device:
        # Unauthenticated requests are rejected by default; /api/auth/session returns 200 with user=None
        assert alice_browser.get("/api/projects").status_code == 401
        assert alice_browser.get("/api/auth/me").status_code == 401
        assert alice_browser.get("/api/auth/session").json() == {"user": None}

        # Password validation & email normalization; first user becomes admin
        assert alice_browser.post("/api/auth/register", json={"email": "a@b.co", "password": "short"}).status_code == 422
        reg = alice_browser.post(
            "/api/auth/register",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Chrome/128.0.0.0 Safari/537.36"},
            json={"email": "  Alice@Example.com ", "password": "strong-password-1", "name": "Алиса"},
        )
        assert reg.status_code == 201
        me = reg.json()
        assert me["email"] == "alice@example.com"
        assert me["role"] == "admin"
        assert me["has_password"] is True and me["has_google"] is False

        # Duplicate email rejected
        dup = second_device.post(
            "/api/auth/register",
            json={"email": "alice@example.com", "password": "another-password"},
        )
        assert dup.status_code == 409

        # Wrong password rejected; correct login on second device creates second session
        assert second_device.post("/api/auth/login", json={"email": "alice@example.com", "password": "wrong-password"}).status_code == 401
        login2 = second_device.post(
            "/api/auth/login",
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile Safari/604.1"},
            json={"email": "ALICE@example.com", "password": "strong-password-1"},
        )
        assert login2.status_code == 200

        # Active sessions list shows both devices with parsed UA and current marker
        sessions = alice_browser.get("/api/auth/sessions").json()
        assert len(sessions) == 2
        assert sessions[0]["is_current"] is True and sessions[0]["device"] == "Chrome · macOS"
        assert sessions[1]["is_current"] is False and sessions[1]["device"] == "Safari · iOS"

        # CSRF check blocks untrusted cross-origin mutations on cookie auth
        csrf = alice_browser.patch(
            "/api/auth/me",
            headers={"Origin": "https://evil.attacker.example"},
            json={"name": "Hacked"},
        )
        assert csrf.status_code == 403

        # Trusted origin works; email change requires current password
        assert alice_browser.patch("/api/auth/me", json={"email": "alice2@example.com"}).status_code == 401
        updated = alice_browser.patch(
            "/api/auth/me",
            headers={"Origin": "http://localhost:3000"},
            json={"name": "Алиса К.", "email": "alice2@example.com", "current_password": "strong-password-1"},
        )
        assert updated.status_code == 200 and updated.json()["email"] == "alice2@example.com"

        # Changing password revokes other sessions while keeping current session alive
        pw_res = alice_browser.post(
            "/api/auth/password",
            json={"current_password": "strong-password-1", "new_password": "new-strong-password-2"},
        )
        assert pw_res.status_code == 200 and pw_res.json()["revoked_sessions"] == 1
        assert alice_browser.get("/api/auth/me").status_code == 200

        # Second device session was revoked and gets its stale cookie cleared on 401
        revoked_check = second_device.get("/api/auth/me")
        assert revoked_check.status_code == 401
        assert "fl_session=" in revoked_check.headers.get("set-cookie", "")


def test_multi_user_isolation_and_admin_controls(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as admin_client, TestClient(app) as bob_client:
        admin = admin_client.post(
            "/api/auth/register",
            json={"email": "admin@example.com", "password": "admin-password-1", "name": "Admin"},
        ).json()
        bob = bob_client.post(
            "/api/auth/register",
            json={"email": "bob@example.com", "password": "bob-password-123", "name": "Bob"},
        ).json()
        assert admin["role"] == "admin" and bob["role"] == "user"

        # Projects, uploads, avatars, and jobs are strictly isolated per user
        admin_proj = admin_client.post("/api/projects", json={"title": "Ролик Админа"}).json()
        bob_proj = bob_client.post("/api/projects", json={"title": "Ролик Боба"}).json()

        assert [p["title"] for p in admin_client.get("/api/projects").json()] == ["Ролик Админа"]
        assert [p["title"] for p in bob_client.get("/api/projects").json()] == ["Ролик Боба"]
        assert bob_client.patch(f"/api/projects/{admin_proj['id']}", json={"title": "Украл"}).status_code == 404
        assert bob_client.delete(f"/api/projects/{admin_proj['id']}").status_code == 404

        # Bob cannot submit a job into Admin's project or use Admin's upload key
        admin_upload = admin_client.post("/api/uploads", files={"file": ("a.png", b"png")}).json()["key"]
        assert bob_client.post(
            "/api/jobs",
            json={"mode": "t2v", "project_id": admin_proj["id"], "rows": [{"name": "1", "prompt": "p"}]},
        ).status_code == 404
        assert bob_client.post(
            "/api/avatars",
            json={"name": "Чужой", "image_keys": [admin_upload]},
        ).status_code == 422

        admin_job = admin_client.post(
            "/api/jobs",
            json={"mode": "t2v", "project_id": admin_proj["id"], "rows": [{"name": "1", "prompt": "p"}]},
        ).json()
        assert bob_client.get("/api/jobs").json() == []
        assert bob_client.get(f"/api/jobs/{admin_job['id']}").status_code == 404

        # Internal Dolphin accounts endpoint is admin-only
        assert bob_client.get("/api/accounts").status_code == 403
        assert admin_client.get("/api/accounts").status_code == 200
        assert bob_client.get("/api/admin/users").status_code == 403
        assert len(admin_client.get("/api/admin/users").json()) == 2

        # Admin cannot block or demote themselves, but blocking Bob immediately kills Bob's session
        assert admin_client.patch(f"/api/admin/users/{admin['id']}", json={"is_active": False}).status_code == 422
        assert admin_client.patch(f"/api/admin/users/{bob['id']}", json={"is_active": False}).status_code == 200
        assert bob_client.get("/api/projects").status_code == 401
        assert bob_client.post("/api/auth/login", json={"email": "bob@example.com", "password": "bob-password-123"}).status_code == 403


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
            "email_code": False,
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
        loc = urlparse(start.headers["location"])
        qs = parse_qs(loc.query)
        assert qs["code_challenge_method"] == ["S256"]
        state = qs["state"][0]

        # Tampered state fails cleanly and redirects to /login?auth_error=...
        bad_cb = client.get("/api/auth/google/callback?code=auth-code-xyz&state=tampered", follow_redirects=False)
        assert bad_cb.status_code == 302 and "auth_error=" in bad_cb.headers["location"]

        # Restart and complete valid callback
        start = client.get("/api/auth/google/start?next=/avatars", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        cb = client.get(f"/api/auth/google/callback?code=auth-code-xyz&state={state}", follow_redirects=False)
        assert cb.status_code == 302
        assert cb.headers["location"] == "http://localhost:3000/avatars"

        me = client.get("/api/auth/me").json()
        assert me["email"] == "googleuser@example.com"
        assert me["has_google"] is True and me["has_password"] is False

        # Cannot unlink Google without setting a password first
        assert client.delete("/api/auth/google").status_code == 422
        assert client.post("/api/auth/password", json={"new_password": "added-local-password"}).status_code == 200
        assert client.delete("/api/auth/google").json()["has_google"] is False


def test_resend_email_otp_password_reset_and_verification(tmp_path: Path):
    outbox: list[EmailMessage] = []

    async def capture(msg: EmailMessage) -> None:
        outbox.append(msg)

    mailer = ResendMailer(sender=capture)
    app = create_app(_settings(tmp_path, frontend_url="http://localhost:3000"), mailer=mailer)

    with TestClient(app) as otp_client, TestClient(app) as pw_client, TestClient(app) as other_device:
        assert otp_client.get("/api/auth/providers").json() == {
            "email": True,
            "google": False,
            "email_code": True,
            "password_reset": True,
        }

        # 1. Passwordless 6-digit Email OTP registration & login
        assert otp_client.post("/api/auth/email-code/send", json={"email": "otp@example.com"}).status_code == 200
        assert len(outbox) == 1 and outbox[-1].to == "otp@example.com"
        otp_code = re.search(r"\b(\d{6})\b", outbox[-1].text).group(1)

        assert (
            otp_client.post("/api/auth/email-code/verify", json={"email": "otp@example.com", "code": "000000"}).status_code
            == 401
        )
        verified_login = otp_client.post(
            "/api/auth/email-code/verify",
            json={"email": "otp@example.com", "code": otp_code},
        )
        assert verified_login.status_code == 200
        assert verified_login.json()["email_verified"] is True
        assert verified_login.json()["role"] == "admin"
        # One-time code cannot be replayed
        assert (
            other_device.post("/api/auth/email-code/verify", json={"email": "otp@example.com", "code": otp_code}).status_code
            == 401
        )

        # 2. Email + Password registration sends verification email; verify by code, then change email & verify by link
        reg = pw_client.post(
            "/api/auth/register",
            json={"email": "lena@example.com", "password": "lena-password-1", "name": "Лена"},
        )
        assert reg.status_code == 201 and reg.json()["email_verified"] is False
        assert outbox[-1].to == "lena@example.com"
        ver_code = re.search(r"\b(\d{6})\b", outbox[-1].text).group(1)

        assert pw_client.post("/api/auth/email/verify/confirm", json={"code": ver_code}).status_code == 200
        assert pw_client.get("/api/auth/me").json()["email_verified"] is True

        # Changing email resets email_verified and dispatches a fresh verification link
        patched = pw_client.patch(
            "/api/auth/me",
            json={"email": "lena2@example.com", "current_password": "lena-password-1"},
        )
        assert patched.status_code == 200 and patched.json()["email_verified"] is False
        ver_token = parse_qs(urlparse(re.search(r"http://localhost:3000/verify-email\?token=[^\s]+", outbox[-1].text).group(0)).query)["token"][0]

        # 1-click verification link works even from an unauthenticated client
        assert other_device.post("/api/auth/email/verify/confirm", json={"token": ver_token}).status_code == 200
        assert pw_client.get("/api/auth/me").json()["email_verified"] is True

        # 3. Password reset: non-enumerating for unknown email, revokes existing sessions for real user
        before_count = len(outbox)
        assert other_device.post("/api/auth/password/forgot", json={"email": "nobody@example.com"}).status_code == 200
        assert len(outbox) == before_count

        assert other_device.post("/api/auth/password/forgot", json={"email": "lena2@example.com"}).status_code == 200
        assert len(outbox) == before_count + 1
        reset_token = parse_qs(urlparse(re.search(r"http://localhost:3000/reset-password\?token=[^\s]+", outbox[-1].text).group(0)).query)["token"][0]

        reset_res = other_device.post(
            "/api/auth/password/reset",
            json={"token": reset_token, "new_password": "lena-new-password-99"},
        )
        assert reset_res.status_code == 200
        assert other_device.get("/api/auth/me").json()["email"] == "lena2@example.com"
        # Old session in pw_client was revoked on password reset
        assert pw_client.get("/api/auth/me").status_code == 401
        # Token cannot be reused
        assert (
            pw_client.post("/api/auth/password/reset", json={"token": reset_token, "new_password": "another-password"}).status_code
            == 401
        )


def test_gateway_forward_auth_cross_node_sync(tmp_path: Path):
    shared_secret = "shared-prod-gateway-secret-1234567890"
    gw_dir = tmp_path / "gateway"
    wk_dir = tmp_path / "worker"
    gw_dir.mkdir()
    wk_dir.mkdir()

    # Seed legacy unowned project on worker node before auth is enabled
    seed_app = create_app(_settings(wk_dir, auth_required=False))
    with TestClient(seed_app) as seed_client:
        assert seed_client.post("/api/projects", json={"title": "Legacy Worker Project"}).status_code == 201

    gw_app = create_app(_settings(gw_dir, auth_secret=shared_secret, frontend_url="https://motionpr.org"))
    wk_app = create_app(_settings(wk_dir, auth_secret=shared_secret, frontend_url="https://motionpr.org"))

    with TestClient(gw_app) as admin_gw, TestClient(gw_app) as user_gw, TestClient(wk_app) as worker:
        assert admin_gw.get("/api/auth/verify").status_code == 401

        reg_admin = admin_gw.post(
            "/api/auth/register",
            json={"email": "admin@motionpr.org", "password": "admin-password-1", "name": "Admin"},
        )
        assert reg_admin.status_code == 201
        admin_id = reg_admin.json()["id"]

        # CSRF check enforced when Caddy forwards X-Forwarded-Method: POST with untrusted Origin
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

        # Worker node accepts signed gateway header, creates matching user id, and claims legacy rows
        worker_projects = worker.get("/api/projects", headers={"X-Fl-Gateway-Auth": gw_hdr})
        assert worker_projects.status_code == 200
        assert [p["title"] for p in worker_projects.json()] == ["Legacy Worker Project"]
        assert worker.get("/api/auth/me", headers={"X-Fl-Gateway-Auth": gw_hdr}).json()["id"] == admin_id

        # Second regular user gets isolated workspace on worker node
        assert (
            user_gw.post(
                "/api/auth/register",
                json={"email": "member@motionpr.org", "password": "member-password-1", "name": "Member"},
            ).status_code
            == 201
        )
        user_hdr = user_gw.get("/api/auth/verify").headers["X-Fl-Gateway-Auth"]
        assert worker.get("/api/projects", headers={"X-Fl-Gateway-Auth": user_hdr}).json() == []

        # Forged header is rejected
        assert worker.get("/api/projects", headers={"X-Fl-Gateway-Auth": f"{gw_hdr}tampered"}).status_code == 401

