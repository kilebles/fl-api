import hashlib
import time

_SAPISID_VARIANTS: tuple[tuple[str, str], ...] = (
    ("SAPISID", "SAPISIDHASH"),
    ("__Secure-1PAPISID", "SAPISID1PHASH"),
    ("__Secure-3PAPISID", "SAPISID3PHASH"),
)

SCHEME_BEARER = "Bearer"
SCHEME_SAPISIDHASH = "SAPISIDHASH"


def auth_scheme(authorization: str) -> str:
    """Return the scheme part of an Authorization header value."""
    return authorization.split(" ", 1)[0] if authorization else ""


def sapisidhash_header(cookies: dict[str, str], origin: str) -> str | None:
    """Build a Google first-party Authorization header from SAPISID cookies.

    Mirrors what Google web apps send to *-pa.googleapis.com when they are not
    using OAuth bearer tokens: one hash per cookie variant, all bound to the
    calling origin and the current unix timestamp.
    """
    ts = int(time.time())
    parts: list[str] = []
    for cookie_name, label in _SAPISID_VARIANTS:
        value = cookies.get(cookie_name)
        if not value:
            continue
        digest = hashlib.sha1(f"{ts} {value} {origin}".encode()).hexdigest()
        parts.append(f"{label} {ts}_{digest}")
    return " ".join(parts) or None
