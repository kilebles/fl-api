"""Authentication, password hashing (Argon2id), mandatory email verification, opaque sessions, and user accounts."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from loguru import logger
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, Database

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30}[a-z0-9])?$")
_HASHER = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)
_DUMMY_HASH = _HASHER.hash("dummy-timing-guard-password")
_LAST_SEEN_THROTTLE = timedelta(minutes=5)


class AuthError(ValueError):
    """Domain validation or conflict error in account operations."""


class InvalidCredentialsError(AuthError):
    pass


class EmailAlreadyExistsError(AuthError):
    pass


class UsernameAlreadyExistsError(AuthError):
    pass


class EmailNotVerifiedError(AuthError):
    def __init__(self, email: str, message: str = "Подтвердите почту кодом из письма") -> None:
        super().__init__(message)
        self.email = email


class AccountDisabledError(AuthError):
    pass


class RateLimitedError(AuthError):
    pass


class UserNotFoundError(LookupError):
    pass


class SessionNotFoundError(LookupError):
    pass


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(32), unique=True, index=True, default=None)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(500), default=None)
    password_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    google_sub: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, default=None)
    role: Mapped[str] = mapped_column(String(16), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class EmailChallengeRecord(Base):
    __tablename__ = "auth_email_challenges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(24), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, default=None)
    secret_hash: Mapped[str] = mapped_column(String(64), index=True)
    aux_hash: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SessionRecord(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


@dataclass(slots=True)
class Principal:
    """Caller identity resolved from a session cookie, gateway header, or service API token."""

    user: UserRecord | None = None
    session: SessionRecord | None = None
    is_service: bool = False

    @property
    def user_id(self) -> str | None:
        return self.user.id if self.user is not None else None

    @property
    def is_admin(self) -> bool:
        return self.is_service or (self.user is not None and self.user.role == "admin")


def normalize_email(email: str) -> str:
    """Validate and normalize an email address to lowercase."""
    cleaned = email.strip().lower()
    if not cleaned or len(cleaned) > 320 or not _EMAIL_RE.match(cleaned):
        raise AuthError("Некорректный адрес электронной почты")
    return cleaned


def normalize_username(username: str) -> str:
    """Validate and normalize an account username (`3..32` lowercase `[a-z0-9._-]`)."""
    cleaned = username.strip().lstrip("@").lower()
    if len(cleaned) < 3 or len(cleaned) > 32 or ".." in cleaned or not _USERNAME_RE.match(cleaned):
        raise AuthError("Username должен содержать от 3 до 32 символов (a-z, 0-9, ., _, -)")
    return cleaned


def display_username(user: UserRecord) -> str:
    if user.username:
        return user.username
    fallback = re.sub(r"[^a-z0-9._-]", "", (user.name or user.email.split("@", 1)[0]).lower()).strip("._-")
    return fallback[:32] if len(fallback) >= 3 else f"user_{user.id[:6]}"


def validate_password(password: str) -> str:
    """Enforce password length bounds (OWASP ASVS: 8..128 characters, no composition gimmicks)."""
    if len(password) < 8:
        raise AuthError("Пароль должен содержать минимум 8 символов")
    if len(password) > 128:
        raise AuthError("Пароль не может быть длиннее 128 символов")
    return password


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def describe_user_agent(user_agent: str | None) -> str:
    """Summarize a raw User-Agent string into `Browser · OS` for the session list."""
    if not user_agent:
        return "Неизвестное устройство"
    ua = user_agent
    if "Edg/" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "YaBrowser/" in ua:
        browser = "Яндекс Браузер"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua and "Version/" in ua:
        browser = "Safari"
    else:
        browser = "Браузер"

    if "iPhone" in ua or "iPad" in ua or "iOS" in ua:
        os_name = "iOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "macOS"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = ""

    return f"{browser} · {os_name}" if os_name else browser


def build_pkce_pair() -> tuple[str, str]:
    """Return `(code_verifier, code_challenge)` for OAuth 2.0 PKCE S256."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def sign_oauth_state(payload: dict[str, Any], secret: str) -> str:
    """Encode and HMAC-sign an OAuth / gateway state dictionary."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_oauth_state(token: str, secret: str, max_age_seconds: int = 600) -> dict[str, Any] | None:
    """Verify an HMAC-signed OAuth / gateway state token and enforce `iat` freshness."""
    if "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(sig, expected):
        return None
    padding = "=" * (-len(body) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(body + padding).decode("utf-8"))
    except Exception:
        return None
    iat = data.get("iat")
    if not isinstance(iat, (int, float)) or time.time() - iat > max_age_seconds:
        return None
    return data


class LoginRateLimiter:
    """In-memory sliding-window rate limiter against credential stuffing and brute force."""

    def __init__(self, limit: int = 10, window_seconds: float = 900.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def check(self, *keys: str) -> None:
        now = time.monotonic()
        cutoff = now - self._window
        for key in keys:
            if not key:
                continue
            bucket = self._hits.get(key)
            if not bucket:
                continue
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                raise RateLimitedError("Слишком много попыток. Подождите несколько минут.")

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        cutoff = now - self._window
        for key in keys:
            if not key:
                continue
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            bucket.append(now)

    def clear(self, *keys: str) -> None:
        for key in keys:
            self._hits.pop(key, None)


class AuthService:
    """User accounts (`username` + verified `email` + Argon2id / Google OAuth) and opaque sessions."""

    def __init__(
        self,
        db: Database,
        session_days: int = 30,
        admin_emails: list[str] | None = None,
        first_user_admin: bool = True,
        email_cooldown_seconds: int = 45,
    ) -> None:
        self._session = db.session
        self._lifetime = timedelta(days=max(session_days, 1))
        self._admin_emails = {e.strip().lower() for e in (admin_emails or []) if e.strip()}
        self._first_user_admin = first_user_admin
        self._email_cooldown = timedelta(seconds=max(email_cooldown_seconds, 0))
        self.limiter = LoginRateLimiter()

    async def register(
        self,
        email: str,
        username: str,
        password: str,
        ip_address: str | None = None,
    ) -> tuple[UserRecord, str, str]:
        """Create (or refresh unverified) account and issue a mandatory email verification code + URL token."""
        norm_email = normalize_email(email)
        norm_username = normalize_username(username)
        validate_password(password)
        ip_key = f"reg_ip:{ip_address or 'local'}"
        self.limiter.check(ip_key)
        pw_hash = _HASHER.hash(password)
        now = datetime.now()
        raw_token = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"

        async with self._session() as session:
            await self._prune_expired_challenges(session, now)
            # Reclaim abandoned unverified registrations older than 24h
            await session.execute(
                delete(UserRecord).where(
                    UserRecord.email_verified_at.is_(None),
                    UserRecord.google_sub.is_(None),
                    UserRecord.created_at <= now - timedelta(hours=24),
                )
            )

            existing_email = await session.scalar(select(UserRecord).where(UserRecord.email == norm_email))
            if existing_email is not None and existing_email.email_verified_at is not None:
                self.limiter.record_failure(ip_key)
                raise EmailAlreadyExistsError("Аккаунт с такой почтой уже зарегистрирован")

            existing_username = await session.scalar(select(UserRecord).where(UserRecord.username == norm_username))
            if existing_username is not None and (existing_email is None or existing_username.id != existing_email.id):
                if (
                    existing_username.email_verified_at is None
                    and existing_username.google_sub is None
                    and existing_username.created_at <= now - timedelta(minutes=30)
                ):
                    await session.delete(existing_username)
                    await session.flush()
                else:
                    self.limiter.record_failure(ip_key)
                    raise UsernameAlreadyExistsError("Этот username уже занят")

            await self._enforce_cooldown(session, "verify_email", norm_email, now)

            if existing_email is not None:
                user = existing_email
                user.username = norm_username
                user.name = norm_username
                user.password_hash = pw_hash
                user.updated_at = now
            else:
                user = UserRecord(
                    id=uuid.uuid4().hex[:16],
                    username=norm_username,
                    email=norm_email,
                    name=norm_username,
                    password_hash=pw_hash,
                    role="admin" if norm_email in self._admin_emails else "user",
                    is_active=True,
                    email_verified_at=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)

            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                raise UsernameAlreadyExistsError("Этот username или почта уже используются") from None

            await session.execute(
                delete(EmailChallengeRecord).where(
                    EmailChallengeRecord.purpose == "verify_email",
                    EmailChallengeRecord.user_id == user.id,
                )
            )
            session.add(
                EmailChallengeRecord(
                    id=uuid.uuid4().hex[:16],
                    purpose="verify_email",
                    email=norm_email,
                    user_id=user.id,
                    secret_hash=hash_token(f"verify_email:{norm_email}:{code}"),
                    aux_hash=hash_token(raw_token),
                    attempts=0,
                    created_at=now,
                    expires_at=now + timedelta(hours=24),
                )
            )
            await session.commit()

        logger.info(f"[auth] pending registration {user.id} (@{user.username}, {user.email}) awaiting email verification")
        return user, raw_token, code

    async def login(
        self,
        identifier: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserRecord, SessionRecord, str]:
        """Authenticate with `username` or `email` + `password`; blocks unverified emails."""
        raw_ident = identifier.strip().lower()
        ip_key = f"login_ip:{ip_address or 'local'}"
        acct_key = f"login_acct:{raw_ident}"
        self.limiter.check(ip_key, acct_key)

        async with self._session() as session:
            if "@" in raw_ident and not raw_ident.startswith("@"):
                stmt = select(UserRecord).where(UserRecord.email == raw_ident)
            else:
                clean_u = raw_ident.lstrip("@")
                stmt = select(UserRecord).where((UserRecord.username == clean_u) | (UserRecord.email == clean_u))

            user = await session.scalar(stmt)
            target_hash = user.password_hash if (user and user.password_hash) else _DUMMY_HASH
            valid = False
            try:
                valid = _HASHER.verify(target_hash, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                valid = False

            if user is None or not user.password_hash or not valid:
                self.limiter.record_failure(ip_key, acct_key)
                raise InvalidCredentialsError("Неверный логин, почта или пароль")

            if not user.is_active:
                raise AccountDisabledError("Аккаунт заблокирован администратором")

            if user.email_verified_at is None:
                raise EmailNotVerifiedError(user.email)

            now = datetime.now()
            if _HASHER.check_needs_rehash(user.password_hash):
                user.password_hash = _HASHER.hash(password)
                user.updated_at = now
            if user.username is None:
                user.username = await self._generate_unique_username(session, user.name or user.email.split("@", 1)[0])
                user.name = user.username
                user.updated_at = now
            if user.email in self._admin_emails and user.role != "admin":
                user.role = "admin"

            sess, raw_token = self._build_session(user.id, ip_address, user_agent, now)
            session.add(sess)
            await session.commit()

        self.limiter.clear(acct_key)
        return user, sess, raw_token

    async def issue_unverified_email_challenge(
        self,
        email: str,
        ip_address: str | None = None,
        enforce_cooldown: bool = True,
    ) -> tuple[UserRecord, str, str] | None:
        """Issue (or refresh) a 24h email verification code + URL token for an unverified account."""
        norm_email = normalize_email(email)
        self.limiter.check(f"verify_send_ip:{ip_address or 'local'}")
        now = datetime.now()
        raw_token = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"

        async with self._session() as session:
            await self._prune_expired_challenges(session, now)
            user = await session.scalar(select(UserRecord).where(UserRecord.email == norm_email))
            if user is None or not user.is_active or user.email_verified_at is not None:
                self.limiter.record_failure(f"verify_send_ip:{ip_address or 'local'}")
                return None

            if self._email_cooldown.total_seconds() > 0:
                recent = await session.scalar(
                    select(EmailChallengeRecord).where(
                        EmailChallengeRecord.purpose == "verify_email",
                        EmailChallengeRecord.email == norm_email,
                        EmailChallengeRecord.created_at > now - self._email_cooldown,
                    )
                )
                if recent is not None:
                    if enforce_cooldown:
                        raise RateLimitedError("Код уже отправлен — подождите минуту перед повторной отправкой")
                    return None

            await session.execute(
                delete(EmailChallengeRecord).where(
                    EmailChallengeRecord.purpose == "verify_email",
                    EmailChallengeRecord.user_id == user.id,
                )
            )
            session.add(
                EmailChallengeRecord(
                    id=uuid.uuid4().hex[:16],
                    purpose="verify_email",
                    email=norm_email,
                    user_id=user.id,
                    secret_hash=hash_token(f"verify_email:{norm_email}:{code}"),
                    aux_hash=hash_token(raw_token),
                    attempts=0,
                    created_at=now,
                    expires_at=now + timedelta(hours=24),
                )
            )
            await session.commit()
            return user, raw_token, code

    async def confirm_email_verification(
        self,
        token: str | None = None,
        email: str | None = None,
        code: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserRecord, SessionRecord, str]:
        """Confirm email via 6-digit code (`email` + `code`) or 1-click URL `token`, and issue a session."""
        ip_key = f"verify_confirm_ip:{ip_address or 'local'}"
        self.limiter.check(ip_key)
        now = datetime.now()

        async with self._session() as session:
            if token:
                cleaned_tok = token.strip()
                if not cleaned_tok or len(cleaned_tok) > 256:
                    raise InvalidCredentialsError("Ссылка подтверждения недействительна или истекла")
                challenge = await session.scalar(
                    select(EmailChallengeRecord).where(
                        EmailChallengeRecord.purpose == "verify_email",
                        EmailChallengeRecord.aux_hash == hash_token(cleaned_tok),
                        EmailChallengeRecord.expires_at > now,
                    )
                )
                if challenge is None or not challenge.user_id:
                    self.limiter.record_failure(ip_key)
                    raise InvalidCredentialsError("Ссылка подтверждения недействительна или истекла")
            elif email and code:
                norm_email = normalize_email(email)
                clean_code = re.sub(r"\s+", "", code.strip())
                challenge = await session.scalar(
                    select(EmailChallengeRecord).where(
                        EmailChallengeRecord.purpose == "verify_email",
                        EmailChallengeRecord.email == norm_email,
                        EmailChallengeRecord.expires_at > now,
                    )
                )
                if challenge is None or not challenge.user_id:
                    self.limiter.record_failure(ip_key)
                    raise InvalidCredentialsError("Код недействителен или истёк — запросите новый")
                expected = hash_token(f"verify_email:{norm_email}:{clean_code}")
                if not secrets.compare_digest(challenge.secret_hash, expected):
                    challenge.attempts += 1
                    exhausted = challenge.attempts >= 5
                    if exhausted:
                        await session.delete(challenge)
                    await session.commit()
                    self.limiter.record_failure(ip_key)
                    if exhausted:
                        raise InvalidCredentialsError("Превышено число попыток — запросите новый код")
                    raise InvalidCredentialsError("Неверный код подтверждения")
            else:
                raise AuthError("Укажите код подтверждения или токен ссылки")

            user = await session.get(UserRecord, challenge.user_id)
            if user is None or user.email != challenge.email:
                await session.delete(challenge)
                await session.commit()
                raise InvalidCredentialsError("Запрос подтверждения устарел — войдите снова")
            if not user.is_active:
                await session.delete(challenge)
                await session.commit()
                raise AccountDisabledError("Аккаунт заблокирован администратором")

            if user.username is None:
                user.username = await self._generate_unique_username(session, user.name or user.email.split("@", 1)[0])
                user.name = user.username

            user.email_verified_at = now
            user.updated_at = now
            await self._finalize_verified_role_and_claims(session, user)
            await session.delete(challenge)

            sess, raw_session = self._build_session(user.id, ip_address, user_agent, now)
            session.add(sess)
            await session.commit()

        logger.info(f"[auth] verified email for user {user.id} (@{user.username}, role={user.role})")
        return user, sess, raw_session

    async def login_or_register_google(
        self,
        google_sub: str,
        email: str,
        email_verified: bool,
        name: str | None,
        picture: str | None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserRecord, SessionRecord, str]:
        """Sign in or register via verified Google OAuth 2.0 identity."""
        if not google_sub or not email_verified:
            raise AuthError("Почта Google-аккаунта не подтверждена")
        norm_email = normalize_email(email)
        now = datetime.now()
        clean_picture = (picture or "").strip()[:500] or None

        async with self._session() as session:
            user = await session.scalar(select(UserRecord).where(UserRecord.google_sub == google_sub))
            if user is None:
                user = await session.scalar(select(UserRecord).where(UserRecord.email == norm_email))
                if user is not None:
                    if user.google_sub and user.google_sub != google_sub:
                        raise AuthError("К этой почте уже привязан другой аккаунт Google")
                    user.google_sub = google_sub
                    if user.username is None:
                        user.username = await self._generate_unique_username(session, norm_email.split("@", 1)[0])
                        user.name = user.username
                    if user.email_verified_at is None:
                        user.email_verified_at = now
                        await self._finalize_verified_role_and_claims(session, user)
                    if not user.avatar_url and clean_picture:
                        user.avatar_url = clean_picture
                    user.updated_at = now
                else:
                    uname = await self._generate_unique_username(session, norm_email.split("@", 1)[0])
                    user = UserRecord(
                        id=uuid.uuid4().hex[:16],
                        username=uname,
                        email=norm_email,
                        name=uname,
                        avatar_url=clean_picture,
                        google_sub=google_sub,
                        role="admin" if norm_email in self._admin_emails else "user",
                        is_active=True,
                        email_verified_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(user)
                    await session.flush()
                    await self._finalize_verified_role_and_claims(session, user)
                    logger.info(f"[auth] registered Google user {user.id} (@{user.username}, {user.email}, role={user.role})")
            else:
                if user.username is None:
                    user.username = await self._generate_unique_username(session, norm_email.split("@", 1)[0])
                    user.name = user.username
                    user.updated_at = now
                if user.email_verified_at is None and user.email == norm_email:
                    user.email_verified_at = now
                    await self._finalize_verified_role_and_claims(session, user)
                    user.updated_at = now
                if not user.avatar_url and clean_picture:
                    user.avatar_url = clean_picture
                    user.updated_at = now

            if not user.is_active:
                raise AccountDisabledError("Аккаунт заблокирован администратором")
            if norm_email in self._admin_emails and user.role != "admin":
                user.role = "admin"

            sess, raw_token = self._build_session(user.id, ip_address, user_agent, now)
            session.add(sess)
            await session.commit()

        return user, sess, raw_token

    async def link_google(self, user_id: str, google_sub: str, picture: str | None = None) -> UserRecord:
        """Link a Google account to an already-authenticated user."""
        if not google_sub:
            raise AuthError("Не удалось получить идентификатор Google")
        clean_picture = (picture or "").strip()[:500] or None
        async with self._session() as session:
            taken = await session.scalar(select(UserRecord).where(UserRecord.google_sub == google_sub))
            if taken is not None and taken.id != user_id:
                raise AuthError("Этот аккаунт Google уже привязан к другому пользователю")
            user = await session.get(UserRecord, user_id)
            if user is None:
                raise UserNotFoundError(user_id)
            user.google_sub = google_sub
            if not user.avatar_url and clean_picture:
                user.avatar_url = clean_picture
            user.updated_at = datetime.now()
            await session.commit()
            return user

    async def unlink_google(self, user_id: str) -> UserRecord:
        """Unlink Google OAuth if the user still has a password set."""
        async with self._session() as session:
            user = await session.get(UserRecord, user_id)
            if user is None:
                raise UserNotFoundError(user_id)
            if not user.password_hash:
                raise AuthError("Сначала задайте пароль, чтобы не потерять доступ к аккаунту")
            user.google_sub = None
            user.updated_at = datetime.now()
            await session.commit()
            return user

    async def authenticate_session(self, raw_token: str) -> tuple[UserRecord, SessionRecord, bool] | None:
        """Validate a raw session token; returns `(user, session, renewed)` or `None`."""
        if not raw_token or len(raw_token) > 256:
            return None
        digest = hash_token(raw_token)
        now = datetime.now()
        renewed = False

        async with self._session() as session:
            sess = await session.scalar(select(SessionRecord).where(SessionRecord.token_hash == digest))
            if sess is None:
                return None
            if sess.expires_at <= now:
                await session.delete(sess)
                await session.commit()
                return None
            user = await session.get(UserRecord, sess.user_id)
            if user is None or not user.is_active or user.email_verified_at is None:
                await session.delete(sess)
                await session.commit()
                return None

            remaining = sess.expires_at - now
            dirty = False
            if user.username is None:
                user.username = await self._generate_unique_username(session, user.name or user.email.split("@", 1)[0])
                user.name = user.username
                dirty = True
            if remaining < self._lifetime / 2:
                sess.expires_at = now + self._lifetime
                sess.last_seen_at = now
                renewed = True
                dirty = True
            elif now - sess.last_seen_at >= _LAST_SEEN_THROTTLE:
                sess.last_seen_at = now
                dirty = True
            if dirty:
                await session.commit()

            return user, sess, renewed

    async def logout(self, raw_token: str | None) -> None:
        """Invalidate the session corresponding to `raw_token`."""
        if not raw_token:
            return
        digest = hash_token(raw_token)
        async with self._session() as session:
            await session.execute(delete(SessionRecord).where(SessionRecord.token_hash == digest))
            await session.commit()

    async def get_user(self, user_id: str) -> UserRecord:
        async with self._session() as session:
            user = await session.get(UserRecord, user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        return user

    async def ensure_gateway_user(
        self,
        user_id: str,
        email: str,
        username: str = "",
        role: str = "user",
    ) -> UserRecord:
        """Upsert a shadow user authenticated by the upstream auth gateway (`X-Fl-Gateway-Auth`)."""
        norm_email = normalize_email(email)
        clean_role = "admin" if (role == "admin" or norm_email in self._admin_emails) else "user"

        async with self._session() as session:
            user = await session.get(UserRecord, user_id)
            if user is None:
                user = await session.scalar(select(UserRecord).where(UserRecord.email == norm_email))

            if user is None:
                total_users = await session.scalar(
                    select(func.count()).select_from(UserRecord).where(UserRecord.email_verified_at.is_not(None))
                ) or 0
                is_first = total_users == 0
                now = datetime.now()
                uname = await self._generate_unique_username(session, username or norm_email.split("@", 1)[0])
                user = UserRecord(
                    id=user_id,
                    username=uname,
                    email=norm_email,
                    name=uname,
                    role=clean_role,
                    is_active=True,
                    email_verified_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                await session.flush()
                if is_first and clean_role == "admin":
                    await self._claim_unowned_records(session, user.id)
                await session.commit()
                logger.info(f"[auth] provisioned gateway shadow user {user.id} (@{user.username}, role={user.role})")
                return user

            if not user.is_active:
                raise AccountDisabledError("Аккаунт заблокирован администратором")

            dirty = False
            if user.email != norm_email:
                taken = await session.scalar(select(UserRecord).where(UserRecord.email == norm_email))
                if taken is None:
                    user.email = norm_email
                    dirty = True
            if username:
                cand = username.strip().lstrip("@").lower()[:32]
                if cand and user.username != cand:
                    taken_u = await session.scalar(select(UserRecord).where(UserRecord.username == cand))
                    if taken_u is None:
                        user.username = cand
                        user.name = cand
                        dirty = True
            elif user.username is None:
                user.username = await self._generate_unique_username(session, norm_email.split("@", 1)[0])
                user.name = user.username
                dirty = True
            if user.role != clean_role:
                user.role = clean_role
                dirty = True
            if user.email_verified_at is None:
                user.email_verified_at = datetime.now()
                dirty = True
            if dirty:
                user.updated_at = datetime.now()
                await session.commit()
            return user

    async def update_profile(
        self,
        user_id: str,
        username: str | None = None,
        avatar_url: str | None = None,
        clear_avatar: bool = False,
    ) -> UserRecord:
        """Update `username` or avatar (`email` changes go through `request_email_change` + `confirm_email_change`)."""
        now = datetime.now()
        async with self._session() as session:
            user = await session.get(UserRecord, user_id)
            if user is None:
                raise UserNotFoundError(user_id)

            if username is not None:
                norm_username = normalize_username(username)
                if norm_username != user.username:
                    taken = await session.scalar(select(UserRecord).where(UserRecord.username == norm_username))
                    if taken is not None and taken.id != user_id:
                        raise UsernameAlreadyExistsError("Этот username уже занят")
                    user.username = norm_username
                    user.name = norm_username

            if clear_avatar:
                user.avatar_url = None
            elif avatar_url is not None:
                user.avatar_url = avatar_url.strip()[:500] or None

            user.updated_at = now
            await session.commit()
            return user

    async def request_email_change(
        self,
        user_id: str,
        new_email: str,
        current_password: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[str, str]:
        """Send a 6-digit confirmation code to `new_email` before updating `user.email`."""
        norm_email = normalize_email(new_email)
        self.limiter.check(f"email_chg_ip:{ip_address or 'local'}")
        now = datetime.now()
        code = f"{secrets.randbelow(1_000_000):06d}"

        async with self._session() as session:
            await self._prune_expired_challenges(session, now)
            user = await session.get(UserRecord, user_id)
            if user is None:
                raise UserNotFoundError(user_id)
            if norm_email == user.email:
                raise AuthError("Этот адрес уже привязан к вашему аккаунту")

            if user.password_hash:
                if not current_password:
                    raise InvalidCredentialsError("Для смены почты введите текущий пароль")
                try:
                    _HASHER.verify(user.password_hash, current_password)
                except (VerifyMismatchError, VerificationError, InvalidHashError):
                    raise InvalidCredentialsError("Неверный текущий пароль") from None

            taken = await session.scalar(select(UserRecord).where(UserRecord.email == norm_email))
            if taken is not None and taken.id != user_id and taken.email_verified_at is not None:
                raise EmailAlreadyExistsError("Эта почта уже используется другим аккаунтом")

            await self._enforce_cooldown(session, "change_email", norm_email, now)
            await session.execute(
                delete(EmailChallengeRecord).where(
                    EmailChallengeRecord.purpose == "change_email",
                    EmailChallengeRecord.user_id == user_id,
                )
            )
            session.add(
                EmailChallengeRecord(
                    id=uuid.uuid4().hex[:16],
                    purpose="change_email",
                    email=norm_email,
                    user_id=user_id,
                    secret_hash=hash_token(f"change_email:{user_id}:{norm_email}:{code}"),
                    attempts=0,
                    created_at=now,
                    expires_at=now + timedelta(minutes=15),
                )
            )
            await session.commit()

        return norm_email, code

    async def confirm_email_change(self, user_id: str, code: str) -> UserRecord:
        """Verify the 6-digit code sent to the new address and atomically switch `user.email`."""
        clean_code = re.sub(r"\s+", "", code.strip())
        now = datetime.now()

        async with self._session() as session:
            user = await session.get(UserRecord, user_id)
            if user is None:
                raise UserNotFoundError(user_id)

            challenge = await session.scalar(
                select(EmailChallengeRecord).where(
                    EmailChallengeRecord.purpose == "change_email",
                    EmailChallengeRecord.user_id == user_id,
                    EmailChallengeRecord.expires_at > now,
                )
            )
            if challenge is None:
                raise InvalidCredentialsError("Код недействителен или истёк — запросите смену почты повторно")

            expected = hash_token(f"change_email:{user_id}:{challenge.email}:{clean_code}")
            if not secrets.compare_digest(challenge.secret_hash, expected):
                challenge.attempts += 1
                exhausted = challenge.attempts >= 5
                if exhausted:
                    await session.delete(challenge)
                await session.commit()
                if exhausted:
                    raise InvalidCredentialsError("Превышено число попыток — запросите новый код")
                raise InvalidCredentialsError("Неверный код подтверждения")

            taken = await session.scalar(select(UserRecord).where(UserRecord.email == challenge.email))
            if taken is not None and taken.id != user_id:
                if taken.email_verified_at is None and taken.google_sub is None:
                    await session.delete(taken)
                    await session.flush()
                else:
                    await session.delete(challenge)
                    await session.commit()
                    raise EmailAlreadyExistsError("Эта почта уже занята другим аккаунтом")

            user.email = challenge.email
            user.email_verified_at = now
            user.updated_at = now
            await session.delete(challenge)
            await session.commit()
            return user

    async def change_password(
        self,
        user_id: str,
        new_password: str,
        current_password: str | None = None,
        keep_session_id: str | None = None,
        revoke_others: bool = True,
    ) -> int:
        """Set or change password; optionally revokes all other sessions and returns revoked count."""
        validate_password(new_password)
        revoked = 0
        async with self._session() as session:
            user = await session.get(UserRecord, user_id)
            if user is None:
                raise UserNotFoundError(user_id)

            if user.password_hash:
                if not current_password:
                    raise InvalidCredentialsError("Введите текущий пароль")
                try:
                    _HASHER.verify(user.password_hash, current_password)
                except (VerifyMismatchError, VerificationError, InvalidHashError):
                    raise InvalidCredentialsError("Неверный текущий пароль") from None

            user.password_hash = _HASHER.hash(new_password)
            user.updated_at = datetime.now()

            if revoke_others:
                stmt = delete(SessionRecord).where(SessionRecord.user_id == user_id)
                if keep_session_id:
                    stmt = stmt.where(SessionRecord.id != keep_session_id)
                result = await session.execute(stmt)
                revoked = result.rowcount or 0

            await session.commit()
        return revoked

    async def list_sessions(self, user_id: str) -> list[SessionRecord]:
        """Return active sessions of `user_id` sorted newest-active first, pruning expired ones."""
        now = datetime.now()
        async with self._session() as session:
            await session.execute(
                delete(SessionRecord).where(SessionRecord.user_id == user_id, SessionRecord.expires_at <= now)
            )
            await session.commit()
            rows = await session.scalars(
                select(SessionRecord)
                .where(SessionRecord.user_id == user_id, SessionRecord.expires_at > now)
                .order_by(SessionRecord.last_seen_at.desc())
            )
            return list(rows)

    async def revoke_session(self, user_id: str, session_id: str) -> bool:
        """Delete a single session owned by `user_id`."""
        async with self._session() as session:
            sess = await session.get(SessionRecord, session_id)
            if sess is None or sess.user_id != user_id:
                raise SessionNotFoundError(session_id)
            await session.delete(sess)
            await session.commit()
            return True

    async def revoke_other_sessions(self, user_id: str, keep_session_id: str | None) -> int:
        """Revoke every session of `user_id` except `keep_session_id`."""
        async with self._session() as session:
            stmt = delete(SessionRecord).where(SessionRecord.user_id == user_id)
            if keep_session_id:
                stmt = stmt.where(SessionRecord.id != keep_session_id)
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    async def list_users(self) -> list[dict[str, Any]]:
        """Admin view: all users with active session counts."""
        now = datetime.now()
        async with self._session() as session:
            users = list(await session.scalars(select(UserRecord).order_by(UserRecord.created_at.desc())))
            counts = dict(
                (
                    await session.execute(
                        select(SessionRecord.user_id, func.count(SessionRecord.id))
                        .where(SessionRecord.expires_at > now)
                        .group_by(SessionRecord.user_id)
                    )
                ).all()
            )
        return [{"user": u, "active_sessions": int(counts.get(u.id, 0))} for u in users]

    async def admin_update_user(
        self,
        actor_id: str | None,
        target_id: str,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> UserRecord:
        """Admin action: change role or active status of a user (prevents self-demotion/self-block)."""
        if role is not None and role not in ("user", "admin"):
            raise AuthError("Недопустимая роль")
        if actor_id and actor_id == target_id:
            if role == "user":
                raise AuthError("Нельзя снять права администратора с самого себя")
            if is_active is False:
                raise AuthError("Нельзя заблокировать собственный аккаунт")

        async with self._session() as session:
            user = await session.get(UserRecord, target_id)
            if user is None:
                raise UserNotFoundError(target_id)
            if role is not None:
                user.role = role
            if is_active is not None:
                user.is_active = is_active
                if not is_active:
                    await session.execute(delete(SessionRecord).where(SessionRecord.user_id == target_id))
            user.updated_at = datetime.now()
            await session.commit()
            return user

    async def issue_password_reset(self, email: str, ip_address: str | None = None) -> tuple[UserRecord, str] | None:
        """Issue a single-use password reset token (30 min TTL) if an active user exists."""
        norm_email = normalize_email(email)
        self.limiter.check(f"pw_reset_ip:{ip_address or 'local'}", f"pw_reset_email:{norm_email}")
        now = datetime.now()
        raw_token = secrets.token_urlsafe(32)

        async with self._session() as session:
            await self._prune_expired_challenges(session, now)
            user = await session.scalar(select(UserRecord).where(UserRecord.email == norm_email))
            if user is None or not user.is_active:
                self.limiter.record_failure(f"pw_reset_ip:{ip_address or 'local'}")
                return None

            if self._email_cooldown.total_seconds() > 0:
                recent = await session.scalar(
                    select(EmailChallengeRecord).where(
                        EmailChallengeRecord.purpose == "password_reset",
                        EmailChallengeRecord.email == norm_email,
                        EmailChallengeRecord.created_at > now - self._email_cooldown,
                    )
                )
                if recent is not None:
                    return None

            await session.execute(
                delete(EmailChallengeRecord).where(
                    EmailChallengeRecord.purpose == "password_reset",
                    EmailChallengeRecord.user_id == user.id,
                )
            )
            session.add(
                EmailChallengeRecord(
                    id=uuid.uuid4().hex[:16],
                    purpose="password_reset",
                    email=norm_email,
                    user_id=user.id,
                    secret_hash=hash_token(raw_token),
                    attempts=0,
                    created_at=now,
                    expires_at=now + timedelta(minutes=30),
                )
            )
            await session.commit()

        self.limiter.record_failure(f"pw_reset_ip:{ip_address or 'local'}")
        return user, raw_token

    async def reset_password_with_token(
        self,
        raw_token: str,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserRecord, SessionRecord, str]:
        """Consume a password-reset token, set new Argon2id password, verify email, revoke old sessions, and sign in."""
        validate_password(new_password)
        cleaned = raw_token.strip()
        if not cleaned or len(cleaned) > 256:
            raise InvalidCredentialsError("Ссылка для сброса пароля недействительна или устарела")
        digest = hash_token(cleaned)
        now = datetime.now()

        async with self._session() as session:
            challenge = await session.scalar(
                select(EmailChallengeRecord).where(
                    EmailChallengeRecord.purpose == "password_reset",
                    EmailChallengeRecord.secret_hash == digest,
                    EmailChallengeRecord.expires_at > now,
                )
            )
            if challenge is None or not challenge.user_id:
                raise InvalidCredentialsError("Ссылка для сброса пароля недействительна или устарела")

            user = await session.get(UserRecord, challenge.user_id)
            if user is None or not user.is_active:
                await session.delete(challenge)
                await session.commit()
                raise AccountDisabledError("Аккаунт недоступен")

            await session.execute(
                delete(EmailChallengeRecord).where(
                    EmailChallengeRecord.purpose.in_(("password_reset", "verify_email")),
                    EmailChallengeRecord.user_id == user.id,
                )
            )
            await session.execute(delete(SessionRecord).where(SessionRecord.user_id == user.id))

            if user.username is None:
                user.username = await self._generate_unique_username(session, user.name or user.email.split("@", 1)[0])
                user.name = user.username
            user.password_hash = _HASHER.hash(new_password)
            if user.email == challenge.email and user.email_verified_at is None:
                user.email_verified_at = now
                await self._finalize_verified_role_and_claims(session, user)
            user.updated_at = now

            sess, session_token = self._build_session(user.id, ip_address, user_agent, now)
            session.add(sess)
            await session.commit()

        return user, sess, session_token

    async def _finalize_verified_role_and_claims(self, session: Any, user: UserRecord) -> None:
        verified_others = (
            await session.scalar(
                select(func.count())
                .select_from(UserRecord)
                .where(UserRecord.email_verified_at.is_not(None), UserRecord.id != user.id)
            )
            or 0
        )
        is_first_verified = verified_others == 0
        if user.email in self._admin_emails or (is_first_verified and self._first_user_admin):
            user.role = "admin"
        if is_first_verified and user.role == "admin":
            await self._claim_unowned_records(session, user.id)

    async def _generate_unique_username(self, session: Any, seed: str) -> str:
        raw = re.sub(r"[^a-z0-9._-]", "", seed.lower()).strip("._-")
        raw = re.sub(r"\.{2,}", ".", raw)
        base = raw[:24] if len(raw) >= 3 else "user"
        base = base.strip("._-")
        if len(base) < 3:
            base = "user"

        for attempt in range(25):
            candidate = base if attempt == 0 else (f"{base}_{attempt + 1}" if attempt < 15 else f"{base}_{secrets.token_hex(2)}")
            taken = await session.scalar(select(UserRecord).where(UserRecord.username == candidate))
            if taken is None:
                return candidate
        return f"user_{secrets.token_hex(4)}"

    async def _enforce_cooldown(self, session: Any, purpose: str, email: str, now: datetime) -> None:
        if self._email_cooldown.total_seconds() <= 0:
            return
        recent = await session.scalar(
            select(EmailChallengeRecord).where(
                EmailChallengeRecord.purpose == purpose,
                EmailChallengeRecord.email == email,
                EmailChallengeRecord.created_at > now - self._email_cooldown,
            )
        )
        if recent is not None:
            raise RateLimitedError("Письмо уже отправлено — подождите минуту перед повторной отправкой")

    async def _prune_expired_challenges(self, session: Any, now: datetime) -> None:
        await session.execute(delete(EmailChallengeRecord).where(EmailChallengeRecord.expires_at <= now))

    def _build_session(
        self,
        user_id: str,
        ip_address: str | None,
        user_agent: str | None,
        now: datetime,
    ) -> tuple[SessionRecord, str]:
        raw_token = secrets.token_urlsafe(32)
        sess = SessionRecord(
            id=uuid.uuid4().hex[:16],
            user_id=user_id,
            token_hash=hash_token(raw_token),
            ip_address=(ip_address or "")[:64] or None,
            user_agent=(user_agent or "")[:500] or None,
            created_at=now,
            last_seen_at=now,
            expires_at=now + self._lifetime,
        )
        return sess, raw_token

    async def _claim_unowned_records(self, session: Any, user_id: str) -> None:
        """Assign pre-auth rows (`user_id IS NULL`) to the bootstrap admin; system templates stay shared."""
        for table in ("projects", "avatars", "jobs", "prompt_runs", "infographic_runs"):
            await session.execute(
                text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"),
                {"uid": user_id},
            )
