class RateLimitError(Exception):
    """API rate limit exceeded (HTTP 429). Rotate account."""


class ConsecutiveFailuresError(Exception):
    """Too many consecutive 403 errors. Rotate account."""


class GenerationFilteredError(Exception):
    """Generation blocked by a content filter (or rejected with 400). Skip this item permanently."""


class IpProhibitedError(Exception):
    """Generation failed with IP_PROHIBITED — proxy IP is blocked. Rotate account/proxy."""


class GenerationTimedOutError(Exception):
    """Generation failed with PUBLIC_ERROR_VIDEO_GENERATION_TIMED_OUT on the server side."""


class DiskFullError(Exception):
    """No space left on the output disk. Stop the batch instead of retrying or skipping items."""


class InputImageRejectedError(GenerationFilteredError):
    """Video failed with PUBLIC_ERROR_IP_INPUT_IMAGE: the source image shows protected
    intellectual property (logos, known characters). Not a proxy problem — a new source
    image may pass, otherwise the row is skipped like any filtered generation."""


class ImageQuotaError(Exception):
    """Image model daily quota reached for this account. The account can still make videos."""


class VideoAccessDeniedError(Exception):
    """The account has no access to the requested video model (PUBLIC_ERROR_MODEL_ACCESS_DENIED).

    A plan/entitlement problem, not a proxy or reCAPTCHA one: the account can still make
    images, so only its videos are paused — no proxy or account rotation.
    """


class RecaptchaInterfaceError(Exception):
    """grecaptcha.enterprise.execute is missing, or the page wrapped it and the original was not captured.

    A page/script problem, not an account or proxy problem — stop the batch without rotating.
    """
