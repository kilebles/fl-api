"""Prompt templates (style extracted from a brief) and generated image/video prompt pairs."""

from pydantic import BaseModel, Field


class PromptTargets(BaseModel):
    image: str = ""
    video: str = ""


class SubPeriod(BaseModel):
    id: str = ""
    title: str = ""
    when: str = ""


class Pair(BaseModel):
    """One named shot pair (image + video) from the brief's shot structure."""

    number: int = 0
    id: str = ""
    title: str = ""
    intent: str = ""
    frame: str = ""
    people: str = ""
    camera: str = ""
    repeatable: bool = False


class Deterministic(BaseModel):
    image_suffix: str = ""
    video_suffix: str = ""
    negatives: str = ""


class PairOverride(BaseModel):
    pair_id: str = ""
    value: str = ""


class Limits(BaseModel):
    image_max_chars: int = 400
    video_max_chars: int = 600
    pair_overrides: list[PairOverride] = Field(default_factory=list)


class I2VPrompt(BaseModel):
    """Structured prompt template for the image-to-video (i2v) model."""

    title: str = ""
    targets: PromptTargets = PromptTargets()
    sub_periods: list[SubPeriod] = Field(default_factory=list)
    lore: str = ""
    pairs: list[Pair] = Field(default_factory=list)
    color_palette: str = ""
    lighting: str = ""
    textures: str = ""
    image_rules: str = ""
    video_rules: str = ""
    deterministic: Deterministic = Deterministic()
    limits: Limits = Limits()


class GeneratedPair(BaseModel):
    """One generated image/video prompt pair for a paragraph, at a given variation."""

    paragraph_number: int
    pair_number: int
    img: str
    vid: str
    stock_queries: list[str]
    paragraph_text: str

    @property
    def name(self) -> str:
        """Row name in generation jobs and xlsx: `{paragraph}.{pair}`."""
        return f"{self.paragraph_number}.{self.pair_number}"


# (model id, label with output price per 1M tokens), as offered by the old bot.
GENERATION_MODELS: list[tuple[str, str]] = [
    ("gemini-2.5-flash-lite", "Gemini Flash-Lite · $0.40"),
    ("gemini-2.5-flash", "Gemini Flash · $2.50"),
    ("gemini-2.5-pro", "Gemini Pro · $10"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash · $7.50"),
    ("gpt-5-nano", "GPT-5 Nano · $0.40"),
    ("gpt-5.6-luna", "GPT-5.6 Luna · $1.20"),
    ("gpt-4.1-mini", "GPT-4.1 Mini · $1.60"),
    ("gpt-5-mini", "GPT-5 Mini · $2"),
    ("gpt-5.6-terra", "GPT-5.6 Terra · $12"),
]
