from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Logging
    log_level: str = Field(default="SUCCESS", description="Minimum log level (DEBUG, INFO, SUCCESS, WARNING, ERROR, CRITICAL)")
    monitor_url: str | None = Field(default=None, description="flow-stat base URL, e.g. http://192.168.1.100:8080")

    # Dolphin Anty
    anty_token: str = Field(default="", description="Dolphin Anty JWT token")
    anty_local_api: str = Field(default="http://localhost:3001", description="Dolphin Anty Local API base URL")

    # Storage
    accounts_dir: Path = Field(default=Path("accounts"), description="Directory with account_N/ folders")
    accounts_enabled: list[str] = Field(default=[], description='Accounts jobs may use, e.g. ["account_1","account_2"]; empty = every account with a dolphin_id')
    output_dir: Path = Field(default=Path("output"), description="Root directory for job results")
    download_dir: Path = Field(default=Path("work/_tmp"), description="Scratch directory for downloads before they are renamed into a job folder")
    work_dir: Path = Field(default=Path("work"), description="API jobs: local work folders, removed once results are in the object store")
    database_url: str = Field(default="sqlite+aiosqlite:///data/fl-api.db", description="SQLAlchemy async URL of the job database")

    # Result storage: "local" keeps files in storage_dir and serves them from the API; "s3" uses a bucket
    storage: Literal["local", "s3"] = Field(default="local", description="Where inputs and results are kept")
    storage_dir: Path = Field(default=Path("storage"), description="Local storage folder")
    public_url: str = Field(default="http://127.0.0.1:8000", description="Base URL of this API in links to local files")

    # S3 API (Cloudflare R2 in production, SeaweedFS locally), used with STORAGE=s3
    s3_endpoint_url: str | None = Field(default=None, description="e.g. https://<account>.r2.cloudflarestorage.com or http://localhost:9000")
    s3_region: str = Field(default="auto", description="'auto' for R2")
    s3_bucket: str = Field(default="fl-api", description="Bucket for inputs and results")
    s3_access_key: str = Field(default="", description="S3 access key id")
    s3_secret_key: str = Field(default="", description="S3 secret access key")
    s3_url_ttl: int = Field(default=3600, description="Lifetime of presigned download links, seconds")

    # LLM (prompt generation)
    google_api_key: str = Field(default="", description="Gemini API key")
    openai_api_key: str = Field(default="", description="OpenAI API key (gpt-* models)")
    prompt_template_model: str = Field(default="gemini-2.5-flash", description="Model that extracts templates from briefs")
    prompt_parallel_runs: int = Field(default=2, description="Prompt generation runs at once")
    infographics_model: str = Field(default="gemini-2.5-flash", description="Gemini model of the infographics pipeline")
    renderer_dir: Path = Field(default=Path("renderer"), description="Remotion project that renders infographic cards")
    render_concurrency: int | None = Field(default=None, description="Chrome tabs per render (None = Remotion default; 1 on a small server)")

    # Web API
    api_token: str = Field(default="", description="Bearer token required by the web API; empty disables the check")
    cors_origins: list[str] = Field(default=["http://localhost:3000", "http://127.0.0.1:3000"], description="Origins of the web frontend")

    # Flow API
    api_url: str = Field(
        default="https://aisandbox-pa.clients6.google.com/v1/video:batchAsyncGenerateVideoText",
        description="Veo text-to-video endpoint URL",
    )
    polling_url: str = Field(
        default="https://aisandbox-pa.clients6.google.com/v1/video:batchCheckAsyncVideoGenerationStatus",
        description="Video status polling endpoint URL",
    )
    api_timeout: float = Field(default=30.0, description="API request timeout in seconds")
    image_api_timeout: float = Field(default=120.0, description="Timeout for image generation API requests (blocking call)")
    polling_timeout: float = Field(default=600.0, description="Polling timeout in seconds")

    # Flow frontend / labs.google session
    labs_url: str = Field(
        default="https://flow.google.com/",
        description="Flow home URL opened in the browser profile (reCAPTCHA + project discovery)",
    )
    flow_base_url: str = Field(
        default="https://flow.google.com",
        description="Flow frontend origin used for Origin/Referer headers and project pages",
    )
    labs_base_url: str = Field(
        default="https://labs.google",
        description="Legacy Google Labs base URL (next-auth session that issues the API access token, tRPC project/media endpoints)",
    )
    labs_session_refresh_interval: float = Field(
        default=1200.0,
        description="Seconds between labs.google session refreshes (access tokens live ~1h)",
    )

    # reCAPTCHA
    recaptcha_site_key: str = Field(default="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV", description="reCAPTCHA site key")
    recaptcha_action: str = Field(default="VIDEO_GENERATION", description="Default reCAPTCHA action name")
    reload_every_min: int = Field(default=8, description="Minimum tokens before page reload")
    reload_every_max: int = Field(default=25, description="Maximum tokens before page reload")
    reload_timeout: float = Field(default=15.0, description="Timeout in seconds for page reload")

    # Generation defaults
    video_aspect_ratio: str = Field(default="VIDEO_ASPECT_RATIO_LANDSCAPE", description="Video aspect ratio")
    video_model_key: str = Field(default="veo_3_1_t2v_lite_low_priority", description="Text-to-video model key")
    i2v_model_key: str = Field(default="veo_3_1_r2v_lite_low_priority", description="Image-to-video model key")
    flash_t2v_model_key: str = Field(default="abra_t2v_10s", description="Text-to-video model of the flash_t2v and f2v modes")
    flash_i2v_model_key: str = Field(default="abra_r2v_10s", description="Image-to-video model of the flash_i2v mode")
    f2v_model_key: str = Field(default="abra_t2v_10s", description="Frame-to-video model for f2v clips after the first one")
    image_model_name: str = Field(
        default="BELUGA",
        description="Image model for flowMedia:batchGenerateImages; BELUGA = Nano Banana 2.1 in the Flow UI (GEM_PIX_2, HARBOR_SEAL also accepted; NARWHAL retired 2026-10-05)",
    )

    # Rotation
    rotate_every_generations: int = Field(default=300, description="Rotate account after this many successful generations")


@lru_cache
def get_settings() -> Settings:
    return Settings()
