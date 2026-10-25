"""Domain and API models for the Translation & Voiceover service."""

from typing import Literal

from pydantic import BaseModel, Field

VoiceStatus = Literal["ok", "missing", "unknown"]
VoiceModelKey = Literal["s2.1", "drama-3"]
JobMode = Literal["all", "translate", "voice", "mix"]
JobStatusState = Literal["queued", "running", "paused", "stopped", "failed", "done"]
StageState = Literal["waiting", "running", "done", "skipped", "failed", "voice_missing"]
ParagraphFilter = Literal["all", "off_norm", "sped_up", "edited", "tags"]


class VoiceApiError(Exception):
    """Structured API error matching the spec: {"error": {"code": ..., "message": ...}}."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class VoiceEntry(BaseModel):
    id: str
    name: str
    fish_id: str
    langs: list[str] = Field(default_factory=list)
    status: VoiceStatus = "ok"
    checked_at: str | None = None
    sample_url: str
    used_in: list[str] = Field(default_factory=list)


class VoiceProfile(BaseModel):
    id: str
    name: str
    channels: list[str] = Field(default_factory=list)
    voices: dict[str, list[str]] = Field(default_factory=dict)
    default: dict[str, str] = Field(default_factory=dict)
    norms: dict[str, tuple[int, int]] = Field(default_factory=dict)


class CreateProfileRequest(BaseModel):
    name: str
    channels: list[str] = Field(default_factory=list)
    copy_from: str | None = None


class UpdateProfileRequest(BaseModel):
    name: str | None = None
    channels: list[str] | None = None
    voices: dict[str, list[str]] | None = None
    default: dict[str, str] | None = None
    norms: dict[str, list[int]] | None = None


class AddVoiceRequest(BaseModel):
    fish_url: str
    name: str = ""
    langs: list[str] | None = None


class CheckVoicesRequest(BaseModel):
    voice_ids: list[str] | None = None
    profile_id: str | None = None


class TranslationModelInfo(BaseModel):
    id: str
    label: str
    engine: Literal["agy", "claude"]
    badge: str | None = None
    description: str
    est_minutes_per_lang: int


class VoiceModelInfo(BaseModel):
    id: VoiceModelKey
    label: str
    description: str
    supports_emotion_tags: bool
    has_free_tier: bool
    price_usd_per_million_bytes: float
    regular_price_usd: float | None = None
    promo_until: str | None = None


class ModelsResponse(BaseModel):
    translation_models: list[TranslationModelInfo]
    voice_models: list[VoiceModelInfo]


class LanguageInfo(BaseModel):
    code: str
    name: str
    native_name: str
    norm: tuple[int, int]
    is_extra: bool = False


class ScriptSceneItem(BaseModel):
    n: int | None = None
    key: str
    heading: bool = False
    text: str


class ScriptInfo(BaseModel):
    script_id: str
    name: str
    title: str
    source_lang: str
    paragraphs_total: int
    words_total: int
    emotion_tags_count: int
    bytes_total: int
    errors: list[str] = Field(default_factory=list)


class EstimateRequest(BaseModel):
    script_id: str
    mode: JobMode = "all"
    source_lang: str = "EN"
    targets: list[str] = Field(default_factory=list)
    model: str = "gemini-4-argon-medium"
    voice_model: VoiceModelKey = "s2.1"
    split_by_language: bool = False


class EstimateResponse(BaseModel):
    duration_sec: int
    targets_count: int
    max_fish_cost_usd: float
    voice_model: VoiceModelKey
    price_usd_per_million_bytes: float


class TranslateStageStatus(BaseModel):
    state: StageState = "waiting"
    parts_done: int = 0
    parts_total: int = 1
    length_pct: float | None = None
    in_norm: int = 0
    total: int = 0
    tags_issues: int = 0


class VoiceStageStatus(BaseModel):
    state: StageState = "waiting"
    done: int = 0
    total: int = 0
    new: int = 0
    reused: int = 0
    fish_model: Literal["free", "paid", "mixed", "drama"] | None = None
    voice_id: str | None = None
    voice_name: str | None = None


class MixStageStatus(BaseModel):
    state: StageState = "waiting"
    done: int = 0
    total: int = 0
    duration_sec: float | None = None
    sped_up: int | None = None
    max_speedup: float | None = None


class VoiceJobLanguage(BaseModel):
    lang: str
    stage: Literal["translate", "voice", "mix", "done"]
    translate: TranslateStageStatus | None = None
    voice: VoiceStageStatus
    mix: MixStageStatus | None = None


class VoiceJobFile(BaseModel):
    id: str
    kind: Literal["docx", "raw_zip", "mixed_zip", "joined_mp3", "report"]
    lang: str
    name: str
    size: int
    url: str


class VoiceJobSummaryLang(BaseModel):
    length_pct: float | None = None
    in_norm: int = 0
    total: int = 0
    sped_up: int = 0
    max_speedup: float = 1.0
    fish_model: str | None = None
    voice_model: str | None = None
    voice_new: int = 0
    voice_reused: int = 0
    carried: int = 0


class VoiceJobSummary(BaseModel):
    duration_sec: float | None = None
    languages: dict[str, VoiceJobSummaryLang] = Field(default_factory=dict)
    norm_suggestions: dict[str, tuple[int, int]] = Field(default_factory=dict)
    skipped: dict[str, dict[str, str]] = Field(default_factory=dict)


class VoiceJob(BaseModel):
    id: int
    title: str
    script: str
    project_id: str | None = None
    mode: JobMode
    source_lang: str
    targets: list[str]
    profile_id: str
    profile_name: str
    voices: dict[str, str]
    model: str
    voice_model: VoiceModelKey = "s2.1"
    carry_mood: bool = False
    status: JobStatusState
    paragraphs_total: int
    emotion_tags_count: int = 0
    source_duration_sec: float | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    pause_until: str | None = None
    pause_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    waiting_for_job_id: int | None = None
    languages: list[VoiceJobLanguage] = Field(default_factory=list)
    summary: VoiceJobSummary | None = None
    files: list[VoiceJobFile] = Field(default_factory=list)


class CreateVoiceJobRequest(BaseModel):
    script_id: str
    title: str | None = None
    project_id: str | None = None
    mode: JobMode = "all"
    source_lang: str | None = None  # None: the language detected on upload
    targets: list[str] = Field(default_factory=list)
    profile_id: str = "p1"
    voices: dict[str, str] = Field(default_factory=dict)
    model: str = "gemini-4-argon-medium"
    voice_model: VoiceModelKey = "s2.1"
    carry_mood: bool = False
    split_by_language: bool = False


class RevoiceSkippedRequest(BaseModel):
    voices: dict[str, str] = Field(default_factory=dict)


class ParagraphAudioTiming(BaseModel):
    source_sec: float | None = None
    slot_sec: float | None = None
    track_sec: float | None = None
    speedup: float = 1.0


class VoiceParagraph(BaseModel):
    n: int
    timecode: str
    source: str
    text: str
    length_pct: float
    norm: tuple[int, int]
    in_norm: bool
    edited: bool = False
    tags_ok: bool = True
    tags_problem: str | None = None
    carried_mood: str | None = None
    chars_delta: int = 0
    audio: ParagraphAudioTiming | None = None
    audio_url: str | None = None
    source_audio_url: str | None = None
    version: int = 1


class PatchParagraphRequest(BaseModel):
    text: str
    version: int = 1


class ParagraphLengthRequest(BaseModel):
    text: str


class ParagraphLengthResponse(BaseModel):
    length_pct: float
    norm: tuple[int, int]
    in_norm: bool
    tags_ok: bool
    tags_problem: str | None = None
    chars_delta: int


class CompareVoiceVariant(BaseModel):
    label: str
    model: str
    audio_url: str | None = None
    sec: float | None = None
    error: str | None = None


class SlotUsage(BaseModel):
    used: int
    total: int


class QueueStatusResponse(BaseModel):
    running: int
    queued: int
    slots: dict[str, SlotUsage]
    worker_online: bool = False  # the translator worker that runs the jobs is up (queue/worker.alive is fresh)
