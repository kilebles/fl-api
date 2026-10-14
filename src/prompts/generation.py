"""Brief → template, and template + scenario paragraphs → image/video prompt pairs."""

import asyncio
import re
from collections.abc import Awaitable, Callable

from loguru import logger

from src.prompts.builder import (
    ExtractedSubject,
    ParagraphResponse,
    SubjectExtractionResponse,
    SummaryResponse,
    apply_deterministic_suffixes,
    build_stock_queries_by_paragraph,
    build_subject_extraction_prompt,
    build_summary_prompt,
    build_system_prompt,
    build_user_prompt,
    match_sub_period,
    paragraph_response_schema,
    subject_extraction_schema,
    summary_response_schema,
    validate_paragraph_response,
)
from src.prompts.llm import LLMClient
from src.prompts.models import GeneratedPair, I2VPrompt, Pair

ProgressCallback = Callable[[int, int], Awaitable[None]]

_SLUG_APOSTROPHES = re.compile(r"['’]")
_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")
_DEFAULT_IMAGE_MAX_CHARS = 400
_DEFAULT_VIDEO_MAX_CHARS = 600

_TEMPLATE_INSTRUCTION = (
    "Extract a structured video-generation prompt template from the following brief.\n"
    "This is an EXTRACTION task, not a creative writing task.\n"
    "Strict rules:\n"
    "- Use only information explicitly present in the source text below.\n"
    "- Do not invent, infer, guess, or add any fact, name, date, or detail that is not "
    "stated in the source text, even if it seems plausible or typical for the topic.\n"
    "- Prefer copying or lightly compressing the source wording over paraphrasing that "
    "could change meaning.\n"
    "- If a field's value is not explicitly present in the source text, leave it at its "
    "default (empty string / 0 / empty list) instead of filling it with a guess.\n"
    "\n"
    "Field-specific guidance:\n"
    "Leave title empty, it is assigned separately.\n"
    "Fill targets.image and targets.video with the short name of the image/video "
    "generation tool the brief is written for (e.g. 'Nano Banana', 'Google Imagen' "
    "for targets.image; 'Google Veo 3.1' for targets.video) — usually named once near "
    "the very start of the brief, such as 'for AI image and video generation tools, "
    "Google Imagen and Google Veo 3.1'. Extract ONLY the short tool name itself, never "
    "any surrounding sentence or rules text — if no tool name is stated, leave empty.\n"
    "Fill sub_periods from any distinct era/sub-period definitions found "
    "(leave id empty, it is assigned separately; title, when=date or time range).\n"
    "Fill pairs from the described shot structure: look for a section that lists "
    "an ordered sequence of named shot pairs, one entry per pair (often marked as "
    "'Pair 1', 'Shot 1', 'Pair 1, ESTABLISHING SHOT.' or similar numbering followed by a "
    "shot type name). Create exactly one pairs entry per such listed pair, in the "
    "same order they appear, however many there are (may be 6, 12, 16, or any other "
    "count) — do not skip any and do not merge multiple pairs into one entry. Leave "
    "number at 0 and id empty, both are assigned separately. For each: "
    "title=the shot type name as given, "
    "intent=one-line purpose/description of what the pair shows, frame=framing/distance "
    "if described, people=who or what appears if described, camera=camera behavior/lens "
    "if described, repeatable=true (this shot structure repeats per paragraph/unit).\n"
    "Fill lore with a concise summary of the narrative/historical context (what happens, "
    "who, when) — do NOT include color palette, lighting, texture, or camera instructions "
    "in lore, those belong in their own dedicated fields below.\n"
    "Fill color_palette with the content of any section describing color palette/tones "
    "per period or scene (often headed 'COLOR PALETTE'), compressed but not summarized "
    "away — keep the specific colors and which period/scene each applies to.\n"
    "Fill lighting with the content of any section describing lighting conditions per "
    "period or scene (often headed 'LIGHTING'), same treatment as color_palette.\n"
    "Fill textures with the content of any section describing materials/textures per "
    "period or scene (often headed 'TEXTURES AND MATERIALS'), same treatment.\n"
    "Fill image_rules and video_rules with the compact instructions for writing "
    "image vs video prompts respectively (camera/lens guidance, composition instructions, "
    "and the 'IMAGE PROMPT INSTRUCTIONS' / 'VIDEO PROMPT INSTRUCTIONS' sections belong "
    "here) — but EXCLUDE any trailing 'end with style tags ...' / 'style tags:' clause, "
    "that belongs in deterministic.image_suffix / video_suffix instead, see below.\n"
    "Fill deterministic.image_suffix and deterministic.video_suffix with the fixed "
    "trailing style-tag clause each prompt should end with — look for phrasing like "
    "'End with style tags, ...' or 'style tags:' near the end of the image/video prompt "
    "instructions, and copy everything after that phrase verbatim (e.g. 'cinematic "
    "historical realism, muted ... palette, practical lighting only, photorealistic 8K, "
    "deep shadow, extreme surface texture, film grain, documentary stillness, no text, "
    "no watermarks, no logos'). If image and video prompts share one identical style-tag "
    "clause, use the same text for both image_suffix and video_suffix.\n"
    "Fill deterministic.negatives by joining the mandatory negative constraints.\n"
    "Leave limits.image_max_chars and limits.video_max_chars at 0 unless the brief "
    "explicitly states a character limit (e.g. 'must not exceed 400 characters') — "
    "defaults are applied separately when none is stated.\n"
)

_CORRECTION = (
    "{emphasis}: your previous attempt at this exact paragraph failed validation "
    "({problem}). Re-read the CURRENT PARAGRAPH text above word by word and generate every "
    "image/video prompt strictly from what it literally describes. Do not mention, reuse, "
    "or default to any era, date, name, or setting detail from any other paragraph, period, "
    "or example — only what this paragraph's own text states."
)


def _slugify(text: str) -> str:
    without_apostrophes = _SLUG_APOSTROPHES.sub("", text.strip().lower())
    return _SLUG_INVALID_CHARS.sub("-", without_apostrophes).strip("-")


async def parse_template(llm: LLMClient, model: str, brief: str, title: str) -> I2VPrompt:
    """Extract a style template from a brief; numbering, ids and default limits are set in code."""
    raw = await llm.json(model, f"{_TEMPLATE_INSTRUCTION}\n\n---\n\n{brief}", I2VPrompt, temperature=0)
    parsed = I2VPrompt.model_validate_json(raw)
    parsed.title = title
    for i, pair in enumerate(parsed.pairs, start=1):
        pair.number = i
        pair.id = _slugify(pair.title or pair.id)
    for period in parsed.sub_periods:
        period.id = _slugify(period.title or period.id)
    if parsed.limits.image_max_chars <= 0:
        parsed.limits.image_max_chars = _DEFAULT_IMAGE_MAX_CHARS
    if parsed.limits.video_max_chars <= 0:
        parsed.limits.video_max_chars = _DEFAULT_VIDEO_MAX_CHARS
    return parsed


async def generate_pairs(
    llm: LLMClient,
    model: str,
    template: I2VPrompt,
    pairs: list[Pair],
    paragraphs: list[str],
    on_progress: ProgressCallback | None = None,
    max_concurrency: int = 6,
) -> list[GeneratedPair]:
    """
    Image/video prompt pairs for every paragraph, `len(pairs)` per paragraph.

    Phase 1 builds the rolling story summary strictly in order (each step needs the
    previous summary, never the generated pairs) while stock-photo subjects are
    extracted from the whole scenario at once. Phase 2 generates the pairs of every
    paragraph concurrently. Era selection, style suffixes and negatives are applied
    in code (see builder), not left to the model.
    """
    system_prompt = build_system_prompt(template, pairs)

    async def build_summaries() -> list[str]:
        before: list[str] = []
        story = ""
        for text in paragraphs:
            before.append(story)
            raw = await llm.json(model, build_summary_prompt(story, text), summary_response_schema(), temperature=0, name="summary_response")
            story = SummaryResponse.model_validate_json(raw).summary
        return before

    async def extract_subjects() -> list[ExtractedSubject]:
        raw = await llm.json(
            model, build_subject_extraction_prompt(paragraphs, template), subject_extraction_schema(), temperature=0, name="subject_extraction_response"
        )
        return SubjectExtractionResponse.model_validate_json(raw).subjects

    async def generate_paragraph(user_prompt: str, paragraph_text: str) -> ParagraphResponse:
        prompt = user_prompt
        parsed = ParagraphResponse()
        for attempt in range(1, 4):
            raw = await llm.json(
                model, prompt, paragraph_response_schema(len(pairs)), temperature=0.7, system=system_prompt, name="paragraph_response"
            )
            parsed = ParagraphResponse.model_validate_json(raw)
            problem = validate_paragraph_response(paragraph_text, parsed)
            if problem is None:
                return parsed
            if attempt == 3:
                logger.warning(f"[prompts] still failing validation after 2 retries, accepting anyway — {problem}")
                return parsed
            logger.warning(f"[prompts] validation failed (attempt {attempt}/3), retrying — {problem}")
            emphasis = "IMPORTANT CORRECTION" if attempt == 1 else "FINAL CORRECTION, be exact this time"
            prompt = f"{user_prompt}\n\n{_CORRECTION.format(emphasis=emphasis, problem=problem)}"
        return parsed

    story_before, subjects = await asyncio.gather(build_summaries(), extract_subjects())
    queries = build_stock_queries_by_paragraph(subjects, paragraphs, template.sub_periods)

    semaphore = asyncio.Semaphore(max_concurrency)
    completed = 0
    lock = asyncio.Lock()

    async def process(index: int, text: str) -> list[GeneratedPair]:
        nonlocal completed
        user_prompt = build_user_prompt(
            index + 1, text, paragraphs[index - 1] if index else "", story_before[index], match_sub_period(text, template.sub_periods)
        )
        async with semaphore:
            parsed = await generate_paragraph(user_prompt, text)
        result = []
        for position, item in enumerate(parsed.pairs[: len(pairs)], start=1):
            final = apply_deterministic_suffixes(item, template)
            result.append(GeneratedPair(
                paragraph_number=index + 1,
                pair_number=position,
                img=final.img,
                vid=final.vid,
                stock_queries=queries.get(index + 1, []),
                paragraph_text=text,
            ))
        if on_progress is not None:
            async with lock:
                completed += 1
                await on_progress(completed, len(paragraphs))
        return result

    grouped = await asyncio.gather(*(process(i, text) for i, text in enumerate(paragraphs)))
    return [pair for group in grouped for pair in group]
