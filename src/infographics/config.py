"""Gemini settings of the infographics pipeline, taken from fl-api's Settings (.env)."""

from __future__ import annotations

from pydantic import BaseModel

from src.core.settings import get_settings as get_app_settings


class GoogleAISettings(BaseModel):
    api_key: str
    model: str = "gemini-2.5-flash"


class Settings(BaseModel):
    google: GoogleAISettings


def get_settings() -> Settings:
    app = get_app_settings()
    if not app.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set")
    return Settings(google=GoogleAISettings(api_key=app.google_api_key, model=app.infographics_model))
