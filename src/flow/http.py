"""Shared request shaping for aisandbox-pa.googleapis.com (the Flow backend)."""

import httpx

API_BASE = "https://aisandbox-pa.googleapis.com/v1"

FORWARDED_BROWSER_HEADERS = (
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "user-agent",
    "x-client-data", "x-browser-validation", "x-goog-api-key", "x-goog-authuser",
)

QUOTA_REACHED = "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED"
TOO_MUCH_TRAFFIC = "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC"
MODEL_ACCESS_DENIED = "PUBLIC_ERROR_MODEL_ACCESS_DENIED"


def api_headers(
    flow_base_url: str,
    authorization: str | None,
    browser_headers: dict[str, str] | None = None,
    referer: str | None = None,
    browser_meta: bool = True,
) -> dict[str, str]:
    """Headers the Flow page sends with its API calls, plus the fingerprint captured from the browser."""
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Origin": flow_base_url,
        "Referer": referer or f"{flow_base_url}/",
        "Priority": "u=1, i",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if browser_meta:
        headers.update({
            "x-browser-channel": "stable",
            "x-browser-copyright": "Copyright 2026 Google LLC. All Rights reserved.",
            "x-browser-year": "2026",
        })
    if authorization:
        headers["Authorization"] = authorization
    if browser_headers:
        headers.update({k: v for k, v in browser_headers.items() if k in FORWARDED_BROWSER_HEADERS})
    return headers


def error_reason(response: httpx.Response) -> str:
    """The `reason` from a Google API error body, or '' when absent."""
    try:
        return response.json().get("error", {}).get("details", [{}])[0].get("reason", "")
    except Exception:
        return ""
