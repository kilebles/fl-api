"""Authentication, session management, Google OAuth 2.0 (PKCE), and admin user endpoints."""

import secrets
import time
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel, Field

from src.auth.mailer import ResendMailer
from src.auth.service import (
    AccountDisabledError,
    AuthError,
    AuthService,
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    Principal,
    RateLimitedError,
    SessionNotFoundError,
    SessionRecord,
    UserNotFoundError,
    UserRecord,
    build_pkce_pair,
    describe_user_agent,
    sign_oauth_state,
    verify_oauth_state,
)
from src.core.settings import Settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
OAUTH_COOKIE = "fl_oauth_state"
GATEWAY_AUTH_HEADER = "X-Fl-Gateway-Auth"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def get_mailer(request: Request) -> ResendMailer:
    return request.app.state.mailer


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https" or request.url.scheme == "https"


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else None


def _origin_tuple(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


def _trusted_origins(request: Request, settings: Settings) -> set[tuple[str, str, int | None]]:
    trusted: set[tuple[str, str, int | None]] = set()
    for raw in (*settings.cors_origins, settings.frontend_url, settings.public_url):
        if raw:
            tup = _origin_tuple(raw)
            if tup:
                trusted.add(tup)
    host_header = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if host_header:
        scheme = "https" if _is_https(request) else request.url.scheme
        tup = _origin_tuple(f"{scheme}://{host_header}")
        if tup:
            trusted.add(tup)
    return trusted


def _verify_csrf(request: Request, settings: Settings, method: str | None = None) -> None:
    """Verify Origin/Referer on unsafe cookie-authenticated requests (OWASP CSRF defense)."""
    if (method or request.method).upper() not in UNSAFE_METHODS:
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    origin = _origin_tuple(source)
    if origin is None:
        raise HTTPException(403, "Недопустимый источник запроса (Origin)")
    trusted = _trusted_origins(request, settings)
    if origin in trusted:
        return
    # Allow localhost <-> 127.0.0.1 equivalence in local loopback development
    if _is_loopback(origin[1]) and any(_is_loopback(t[1]) and t[2] == origin[2] for t in trusted):
        return
    raise HTTPException(403, "Межсайтовый запрос отклонён (CSRF)")


def set_session_cookie(response: Response, request: Request, settings: Settings, raw_token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=raw_token,
        max_age=max(settings.auth_session_days, 1) * 86400,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
    )


async def get_principal(request: Request) -> Principal:
    """Authenticate via signed gateway header, service `API_TOKEN`, or user `fl_session` cookie."""
    settings: Settings = request.app.state.settings
    auth: AuthService = request.app.state.auth

    gw_token = request.headers.get(GATEWAY_AUTH_HEADER, "")
    if gw_token and settings.auth_secret:
        gw_data = verify_oauth_state(gw_token, settings.auth_secret, max_age_seconds=60)
        if gw_data is not None:
            if gw_data.get("svc") is True:
                return Principal(user=None, session=None, is_service=True)
            uid = str(gw_data.get("uid") or "")
            email = str(gw_data.get("email") or "")
            if uid and email:
                user = await auth.ensure_gateway_user(
                    user_id=uid,
                    email=email,
                    name=str(gw_data.get("name") or ""),
                    role=str(gw_data.get("role") or "user"),
                    email_verified=bool(gw_data.get("verified")),
                )
                return Principal(user=user, session=None, is_service=False)

    header = request.headers.get("authorization", "")
    bearer = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    token_param = request.query_params.get("token", "")
    candidate_token = bearer or token_param

    if settings.api_token and candidate_token and secrets.compare_digest(candidate_token, settings.api_token):
        return Principal(user=None, session=None, is_service=True)

    raw_cookie = request.cookies.get(settings.auth_cookie_name) or (
        candidate_token if candidate_token and candidate_token != settings.api_token else ""
    )
    if raw_cookie:
        if request.cookies.get(settings.auth_cookie_name):
            _verify_csrf(request, settings)
        resolved = await auth.authenticate_session(raw_cookie)
        if resolved is not None:
            user, sess, renewed = resolved
            if renewed and request.cookies.get(settings.auth_cookie_name):
                request.state.renew_session_token = raw_cookie
            return Principal(user=user, session=sess, is_service=False)
        if request.cookies.get(settings.auth_cookie_name):
            request.state.clear_auth_cookie = True

    if not settings.auth_required and not settings.api_token:
        return Principal(user=None, session=None, is_service=True)

    raise HTTPException(401, "Требуется авторизация")


async def require_user(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if principal.user is None:
        raise HTTPException(401, "Войдите в аккаунт")
    return principal


async def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if not principal.is_admin:
        raise HTTPException(403, "Доступ только для администратора")
    return principal


Caller = Annotated[Principal, Depends(get_principal)]
LoggedUser = Annotated[Principal, Depends(require_user)]
AdminCaller = Annotated[Principal, Depends(require_admin)]
Auth = Annotated[AuthService, Depends(get_auth)]
Mailer = Annotated[ResendMailer, Depends(get_mailer)]


class ProvidersOut(BaseModel):
    email: bool = True
    google: bool
    email_code: bool = False
    password_reset: bool = False


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    avatar_url: str | None
    avatar_key: str | None = None
    role: Literal["user", "admin"]
    email_verified: bool = False
    has_password: bool
    has_google: bool
    created_at: datetime
    current_session_id: str | None = None


class RegisterIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=256)


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=320)
    current_password: str | None = Field(default=None, max_length=256)
    avatar_key: str | None = Field(default=None, max_length=300)
    clear_avatar: bool = False


class PasswordChangeIn(BaseModel):
    current_password: str | None = Field(default=None, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)
    revoke_other_sessions: bool = True


class PasswordChangedOut(BaseModel):
    ok: bool = True
    revoked_sessions: int


class SessionOut(BaseModel):
    id: str
    device: str
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool


class AdminUserOut(BaseModel):
    id: str
    email: str
    name: str
    avatar_url: str | None
    role: Literal["user", "admin"]
    is_active: bool
    email_verified: bool = False
    has_password: bool
    has_google: bool
    active_sessions: int
    created_at: datetime


class EmailRequestIn(BaseModel):
    email: str = Field(max_length=320)


class EmailCodeVerifyIn(BaseModel):
    email: str = Field(max_length=320)
    code: str = Field(min_length=4, max_length=16)


class PasswordResetConfirmIn(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)


class EmailVerifyConfirmIn(BaseModel):
    token: str | None = Field(default=None, max_length=256)
    code: str | None = Field(default=None, max_length=16)


class AdminUserPatch(BaseModel):
    role: Literal["user", "admin"] | None = None
    is_active: bool | None = None


def _resolve_avatar(raw: str | None, request: Request) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    if raw.startswith("inputs/"):
        return request.app.state.objects.url(raw), raw
    return raw, None


def _user_out(user: UserRecord, request: Request, session_id: str | None = None) -> UserOut:
    avatar_url, avatar_key = _resolve_avatar(user.avatar_url, request)
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        avatar_url=avatar_url,
        avatar_key=avatar_key,
        role="admin" if user.role == "admin" else "user",
        email_verified=bool(user.email_verified_at),
        has_password=bool(user.password_hash),
        has_google=bool(user.google_sub),
        created_at=user.created_at,
        current_session_id=session_id,
    )


def _session_out(sess: SessionRecord, current_id: str | None) -> SessionOut:
    return SessionOut(
        id=sess.id,
        device=describe_user_agent(sess.user_agent),
        ip_address=sess.ip_address,
        user_agent=sess.user_agent,
        created_at=sess.created_at,
        last_seen_at=sess.last_seen_at,
        expires_at=sess.expires_at,
        is_current=sess.id == current_id,
    )


def _safe_next_path(raw: str | None, default: str = "/") -> str:
    if not raw:
        return default
    cleaned = raw.strip()
    if not cleaned.startswith("/") or cleaned.startswith("//") or cleaned.startswith("/\\"):
        return default
    return cleaned


def _frontend_base(request: Request, settings: Settings) -> str:
    referer = request.headers.get("origin") or request.headers.get("referer")
    if referer:
        tup = _origin_tuple(referer)
        trusted = _trusted_origins(request, settings)
        if tup and (tup in trusted or (_is_loopback(tup[1]) and any(_is_loopback(t[1]) and t[2] == tup[2] for t in trusted))):
            port_part = f":{tup[2]}" if tup[2] else ""
            return f"{tup[0]}://{tup[1]}{port_part}"
    return settings.frontend_url.rstrip("/")


def _oauth_callback_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if host:
        return f"{proto}://{host}/api/auth/google/callback"
    return str(request.url_for("google_callback"))


async def exchange_google_code(
    code: str,
    code_verifier: str,
    redirect_uri: str,
    settings: Settings,
) -> dict[str, Any]:
    """Exchange Google authorization code + PKCE verifier and fetch OpenID userinfo."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_res = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        if token_res.status_code != 200:
            raise AuthError("Не удалось подтвердить вход через Google")
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise AuthError("Google не вернул токен доступа")
        info_res = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_res.status_code != 200:
            raise AuthError("Не удалось получить профиль Google")
        return info_res.json()


class SessionCheckOut(BaseModel):
    user: UserOut | None


@auth_router.get("/providers")
async def auth_providers(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    mailer: Mailer,
) -> ProvidersOut:
    return ProvidersOut(
        email=True,
        google=bool(settings.google_client_id and settings.google_client_secret),
        email_code=mailer.enabled,
        password_reset=mailer.enabled,
    )


@auth_router.get("/verify")
async def verify_gateway_auth(
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> dict[str, bool]:
    """Caddy `forward_auth` endpoint: validates session/token and emits signed `X-Fl-Gateway-Auth`."""
    orig_method = (request.headers.get("x-forwarded-method") or request.method).upper()
    if orig_method == "OPTIONS":
        return {"ok": True}

    secret = settings.auth_secret or request.app.state.oauth_secret
    header = request.headers.get("authorization", "")
    bearer = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    orig_uri = request.headers.get("x-forwarded-uri", "")
    fwd_token = parse_qs(urlparse(orig_uri).query).get("token", [""])[0] if "?" in orig_uri else ""
    candidate_token = bearer or request.query_params.get("token", "") or fwd_token

    if settings.api_token and candidate_token and secrets.compare_digest(candidate_token, settings.api_token):
        response.headers[GATEWAY_AUTH_HEADER] = sign_oauth_state({"svc": True, "iat": int(time.time())}, secret)
        return {"ok": True}

    raw_cookie = request.cookies.get(settings.auth_cookie_name) or (
        candidate_token if candidate_token and candidate_token != settings.api_token else ""
    )
    if raw_cookie:
        if request.cookies.get(settings.auth_cookie_name):
            _verify_csrf(request, settings, method=orig_method)
        resolved = await auth.authenticate_session(raw_cookie)
        if resolved is not None:
            user, _, renewed = resolved
            if renewed and request.cookies.get(settings.auth_cookie_name):
                set_session_cookie(response, request, settings, raw_cookie)
            response.headers[GATEWAY_AUTH_HEADER] = sign_oauth_state(
                {
                    "uid": user.id,
                    "email": user.email,
                    "name": user.name,
                    "role": user.role,
                    "verified": bool(user.email_verified_at),
                    "iat": int(time.time()),
                },
                secret,
            )
            return {"ok": True}
        if request.cookies.get(settings.auth_cookie_name):
            request.state.clear_auth_cookie = True

    raise HTTPException(401, "Требуется авторизация")


@auth_router.get("/session")
async def current_session(
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> SessionCheckOut:
    """Non-throwing session probe for SPA bootstrap (`user: null` when signed out)."""
    raw_cookie = request.cookies.get(settings.auth_cookie_name, "")
    if not raw_cookie:
        return SessionCheckOut(user=None)
    resolved = await auth.authenticate_session(raw_cookie)
    if resolved is None:
        clear_session_cookie(response, request, settings)
        return SessionCheckOut(user=None)
    user, sess, renewed = resolved
    if renewed:
        set_session_cookie(response, request, settings, raw_cookie)
    return SessionCheckOut(user=_user_out(user, request, sess.id))


async def _try_send_verification(
    user: UserRecord,
    request: Request,
    auth: AuthService,
    mailer: ResendMailer,
    settings: Settings,
) -> None:
    if not mailer.enabled or user.email_verified_at is not None:
        return
    try:
        _, verify_token, code = await auth.issue_email_verification(
            user.id,
            ip_address=_client_ip(request),
            enforce_cooldown=False,
        )
        frontend = _frontend_base(request, settings)
        url = f"{frontend}/verify-email?{urlencode({'token': verify_token})}"
        await mailer.send_email_verification(user.email, url, code)
    except Exception as exc:
        logger.warning(f"[auth] could not send verification email to {user.email}: {exc}")


@auth_router.post("/register", status_code=201)
async def register(
    body: RegisterIn,
    request: Request,
    response: Response,
    auth: Auth,
    mailer: Mailer,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> UserOut:
    _verify_csrf(request, settings)
    user, sess, raw_token = await auth.register(
        email=body.email,
        password=body.password,
        name=body.name,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await _try_send_verification(user, request, auth, mailer, settings)
    set_session_cookie(response, request, settings, raw_token)
    return _user_out(user, request, sess.id)


@auth_router.post("/login")
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> UserOut:
    _verify_csrf(request, settings)
    user, sess, raw_token = await auth.login(
        email=body.email,
        password=body.password,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookie(response, request, settings, raw_token)
    return _user_out(user, request, sess.id)


@auth_router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    _verify_csrf(request, settings)
    raw_token = request.cookies.get(settings.auth_cookie_name)
    await auth.logout(raw_token)
    clear_session_cookie(response, request, settings)


@auth_router.get("/me")
async def me(principal: LoggedUser, request: Request) -> UserOut:
    assert principal.user is not None
    return _user_out(principal.user, request, principal.session.id if principal.session else None)


@auth_router.patch("/me")
async def patch_me(
    body: ProfilePatch,
    principal: LoggedUser,
    request: Request,
    auth: Auth,
    mailer: Mailer,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> UserOut:
    assert principal.user is not None
    prev_email = principal.user.email
    avatar_key = body.avatar_key
    if avatar_key is not None:
        expected_prefix = f"inputs/{principal.user.id}/"
        if not avatar_key.startswith(expected_prefix) or ".." in avatar_key:
            raise HTTPException(422, "Некорректный файл аватара")
        if not await request.app.state.objects.exists(avatar_key):
            raise HTTPException(422, "Загруженный файл не найден")

    user = await auth.update_profile(
        user_id=principal.user.id,
        name=body.name,
        email=body.email,
        current_password=body.current_password,
        avatar_url=avatar_key,
        clear_avatar=body.clear_avatar,
    )
    if user.email != prev_email:
        await _try_send_verification(user, request, auth, mailer, settings)
    return _user_out(user, request, principal.session.id if principal.session else None)


@auth_router.post("/email-code/send")
async def send_email_code(
    body: EmailRequestIn,
    request: Request,
    auth: Auth,
    mailer: Mailer,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> dict[str, bool]:
    _verify_csrf(request, settings)
    if not mailer.enabled:
        raise HTTPException(503, "Вход по коду из письма не настроен на сервере (RESEND_API_KEY)")
    norm_email, code = await auth.issue_login_otp(body.email, ip_address=_client_ip(request))
    await mailer.send_login_code(norm_email, code)
    return {"ok": True}


@auth_router.post("/email-code/verify")
async def verify_email_code(
    body: EmailCodeVerifyIn,
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> UserOut:
    _verify_csrf(request, settings)
    user, sess, raw_token = await auth.verify_login_otp(
        email=body.email,
        code=body.code,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookie(response, request, settings, raw_token)
    return _user_out(user, request, sess.id)


@auth_router.post("/password/forgot")
async def forgot_password(
    body: EmailRequestIn,
    request: Request,
    auth: Auth,
    mailer: Mailer,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> dict[str, bool]:
    _verify_csrf(request, settings)
    if not mailer.enabled:
        raise HTTPException(503, "Восстановление пароля по почте не настроено на сервере (RESEND_API_KEY)")
    issued = await auth.issue_password_reset(body.email, ip_address=_client_ip(request))
    if issued is not None:
        user, reset_token = issued
        frontend = _frontend_base(request, settings)
        reset_url = f"{frontend}/reset-password?{urlencode({'token': reset_token})}"
        await mailer.send_password_reset(user.email, reset_url)
    return {"ok": True}


@auth_router.post("/password/reset")
async def reset_password(
    body: PasswordResetConfirmIn,
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> UserOut:
    _verify_csrf(request, settings)
    user, sess, raw_token = await auth.reset_password_with_token(
        raw_token=body.token,
        new_password=body.new_password,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookie(response, request, settings, raw_token)
    return _user_out(user, request, sess.id)


@auth_router.post("/email/verify/send")
async def send_email_verification(
    principal: LoggedUser,
    request: Request,
    auth: Auth,
    mailer: Mailer,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> dict[str, bool]:
    assert principal.user is not None
    if not mailer.enabled:
        raise HTTPException(503, "Отправка писем не настроена на сервере (RESEND_API_KEY)")
    user, verify_token, code = await auth.issue_email_verification(
        principal.user.id,
        ip_address=_client_ip(request),
        enforce_cooldown=True,
    )
    frontend = _frontend_base(request, settings)
    verify_url = f"{frontend}/verify-email?{urlencode({'token': verify_token})}"
    await mailer.send_email_verification(user.email, verify_url, code)
    return {"ok": True}


@auth_router.post("/email/verify/confirm")
async def confirm_email_verification(
    body: EmailVerifyConfirmIn,
    request: Request,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> dict[str, Any]:
    _verify_csrf(request, settings)
    if body.token:
        user = await auth.confirm_email_verification_token(body.token)
        return {"ok": True, "email": user.email}
    if body.code:
        raw_cookie = request.cookies.get(settings.auth_cookie_name, "")
        resolved = await auth.authenticate_session(raw_cookie) if raw_cookie else None
        if resolved is None:
            raise HTTPException(401, "Требуется авторизация")
        user = await auth.confirm_email_verification_code(resolved[0].id, body.code)
        return {"ok": True, "email": user.email}
    raise HTTPException(422, "Укажите токен или код подтверждения")


@auth_router.post("/password")
async def change_password(body: PasswordChangeIn, principal: LoggedUser, auth: Auth) -> PasswordChangedOut:
    assert principal.user is not None
    revoked = await auth.change_password(
        user_id=principal.user.id,
        new_password=body.new_password,
        current_password=body.current_password,
        keep_session_id=principal.session.id if principal.session else None,
        revoke_others=body.revoke_other_sessions,
    )
    return PasswordChangedOut(ok=True, revoked_sessions=revoked)


@auth_router.get("/sessions")
async def list_sessions(principal: LoggedUser, auth: Auth) -> list[SessionOut]:
    assert principal.user is not None
    current_id = principal.session.id if principal.session else None
    sessions = await auth.list_sessions(principal.user.id)
    sessions.sort(key=lambda s: (0 if s.id == current_id else 1, -s.last_seen_at.timestamp()))
    return [_session_out(s, current_id) for s in sessions]


@auth_router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    principal: LoggedUser,
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    assert principal.user is not None
    await auth.revoke_session(principal.user.id, session_id)
    if principal.session and principal.session.id == session_id:
        clear_session_cookie(response, request, settings)


@auth_router.post("/sessions/revoke-others")
async def revoke_others(principal: LoggedUser, auth: Auth) -> dict[str, int]:
    assert principal.user is not None
    revoked = await auth.revoke_other_sessions(
        principal.user.id,
        principal.session.id if principal.session else None,
    )
    return {"revoked": revoked}


@auth_router.get("/google/start")
async def google_start(
    request: Request,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    next: str = "/",
    intent: Literal["login", "link"] = "login",
) -> RedirectResponse:
    host_hdr = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if host_hdr == "127.0.0.1" or host_hdr.startswith("127.0.0.1:"):
        localhost_hdr = host_hdr.replace("127.0.0.1", "localhost", 1)
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{request.url.scheme}://{localhost_hdr}{request.url.path}{qs}", status_code=302)

    frontend = _frontend_base(request, settings).replace("://127.0.0.1", "://localhost")
    safe_next = _safe_next_path(next, "/account" if intent == "link" else "/")
    fallback_page = "/account" if intent == "link" else "/login"

    if not settings.google_client_id or not settings.google_client_secret:
        err = urlencode({"auth_error": "Вход через Google ещё не настроен на сервере (GOOGLE_CLIENT_ID)"})
        return RedirectResponse(f"{frontend}{fallback_page}?{err}", status_code=302)

    link_user_id: str | None = None
    if intent == "link":
        raw_cookie = request.cookies.get(settings.auth_cookie_name, "")
        resolved = await auth.authenticate_session(raw_cookie) if raw_cookie else None
        if resolved is None:
            return RedirectResponse(f"{frontend}/login?{urlencode({'next': '/account'})}", status_code=302)
        link_user_id = resolved[0].id

    verifier, challenge = build_pkce_pair()
    nonce = secrets.token_urlsafe(24)
    state_cookie = sign_oauth_state(
        {
            "nonce": nonce,
            "verifier": verifier,
            "next": safe_next,
            "frontend": frontend,
            "link_user_id": link_user_id,
            "iat": int(time.time()),
        },
        request.app.state.oauth_secret,
    )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _oauth_callback_url(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    redirect = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    redirect.set_cookie(
        key=OAUTH_COOKIE,
        value=state_cookie,
        max_age=600,
        httponly=True,
        secure=_is_https(request),
        samesite="lax",
        path="/api/auth/google",
    )
    return redirect


@auth_router.get("/google/callback", name="google_callback")
async def google_callback(
    request: Request,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    raw_state = request.cookies.get(OAUTH_COOKIE, "")
    stored = verify_oauth_state(raw_state, request.app.state.oauth_secret) if raw_state else None
    frontend = (stored.get("frontend") if stored else None) or settings.frontend_url.rstrip("/")
    link_user_id: str | None = stored.get("link_user_id") if stored else None
    safe_next = _safe_next_path(stored.get("next") if stored else None, "/account" if link_user_id else "/")
    error_path = "/account" if link_user_id else "/login"

    def _fail(message: str) -> RedirectResponse:
        sep = "&" if "?" in error_path else "?"
        resp = RedirectResponse(f"{frontend}{error_path}{sep}{urlencode({'auth_error': message})}", status_code=302)
        resp.delete_cookie(OAUTH_COOKIE, path="/api/auth/google")
        return resp

    if error:
        return _fail("Вход через Google отменён")
    if not stored or not state or not code or not secrets.compare_digest(str(stored.get("nonce", "")), state):
        return _fail("Сессия входа истекла — попробуйте снова")

    exchanger = getattr(request.app.state, "google_exchanger", exchange_google_code)
    try:
        profile = await exchanger(code, str(stored["verifier"]), _oauth_callback_url(request), settings)
        sub = str(profile.get("sub") or "")
        email = str(profile.get("email") or "")
        verified = bool(profile.get("email_verified"))
        name = profile.get("name")
        picture = profile.get("picture")

        if link_user_id:
            await auth.link_google(link_user_id, sub, picture)
            resp = RedirectResponse(f"{frontend}{safe_next}", status_code=302)
            resp.delete_cookie(OAUTH_COOKIE, path="/api/auth/google")
            return resp

        _, _, raw_token = await auth.login_or_register_google(
            google_sub=sub,
            email=email,
            email_verified=verified,
            name=name,
            picture=picture,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as e:
        return _fail(str(e))
    except Exception:
        return _fail("Ошибка авторизации через Google")

    resp = RedirectResponse(f"{frontend}{safe_next}", status_code=302)
    resp.delete_cookie(OAUTH_COOKIE, path="/api/auth/google")
    set_session_cookie(resp, request, settings, raw_token)
    return resp


@auth_router.delete("/google")
async def unlink_google(principal: LoggedUser, request: Request, auth: Auth) -> UserOut:
    assert principal.user is not None
    user = await auth.unlink_google(principal.user.id)
    return _user_out(user, request, principal.session.id if principal.session else None)


@admin_router.get("/users")
async def admin_list_users(_admin: AdminCaller, request: Request, auth: Auth) -> list[AdminUserOut]:
    rows = await auth.list_users()
    out: list[AdminUserOut] = []
    for item in rows:
        u: UserRecord = item["user"]
        avatar_url, _ = _resolve_avatar(u.avatar_url, request)
        out.append(
            AdminUserOut(
                id=u.id,
                email=u.email,
                name=u.name,
                avatar_url=avatar_url,
                role="admin" if u.role == "admin" else "user",
                is_active=u.is_active,
                email_verified=bool(u.email_verified_at),
                has_password=bool(u.password_hash),
                has_google=bool(u.google_sub),
                active_sessions=item["active_sessions"],
                created_at=u.created_at,
            )
        )
    return out


@admin_router.patch("/users/{user_id}")
async def admin_patch_user(
    user_id: str,
    body: AdminUserPatch,
    admin: AdminCaller,
    request: Request,
    auth: Auth,
) -> AdminUserOut:
    user = await auth.admin_update_user(
        actor_id=admin.user_id,
        target_id=user_id,
        role=body.role,
        is_active=body.is_active,
    )
    rows = await auth.list_users()
    active_sessions = next((r["active_sessions"] for r in rows if r["user"].id == user.id), 0)
    avatar_url, _ = _resolve_avatar(user.avatar_url, request)
    return AdminUserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        avatar_url=avatar_url,
        role="admin" if user.role == "admin" else "user",
        is_active=user.is_active,
        email_verified=bool(user.email_verified_at),
        has_password=bool(user.password_hash),
        has_google=bool(user.google_sub),
        active_sessions=active_sessions,
        created_at=user.created_at,
    )
