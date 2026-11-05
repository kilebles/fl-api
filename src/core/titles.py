"""Automatic `#2`, `#3`, ... numbering for duplicate project and batch titles."""

import re
from collections.abc import Iterable

_NUMBERED_RE = re.compile(r"^(.*?)\s+#(\d+)$")


def unique_title(base: str, existing: Iterable[str | None], *, max_len: int = 200) -> str:
    """Return `base` if unused in `existing` (case-insensitive), otherwise `<stem> #2`, `<stem> #3`, ..."""
    clean = base.strip()[:max_len]
    if not clean:
        return clean
    taken = {t.strip().casefold() for t in existing if t and t.strip()}
    if clean.casefold() not in taken:
        return clean
    m = _NUMBERED_RE.match(clean)
    if m and m.group(1).strip():
        stem = m.group(1).strip()
        n = int(m.group(2)) + 1
    else:
        stem = clean
        n = 2
    while True:
        suffix = f" #{n}"
        candidate = f"{stem[: max_len - len(suffix)].rstrip()}{suffix}"
        if candidate.casefold() not in taken:
            return candidate
        n += 1
