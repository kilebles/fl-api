"""Document parsing, emotion tag checking, spoken length calculation, and .docx building."""

import json
import os
import re
import time
from pathlib import Path

from docx import Document
from num2words import num2words

from src.voice.models import LanguageInfo

LANG_CONFIG: dict[str, dict] = {
    "EN": {
        "name": "Английский",
        "native": "English",
        "num2words": "en",
        "thousands": [","],
        "decimal": ".",
        "norm": (100, 100),
    },
    "RU": {
        "name": "Русский",
        "native": "Русский",
        "num2words": "ru",
        "thousands": [" "],
        "decimal": ",",
        "norm": (74, 79),
    },
    "ES": {
        "name": "Испанский",
        "native": "Español",
        "num2words": "es",
        "thousands": [".", " "],
        "decimal": ",",
        "norm": (88, 90),
    },
    "PT": {
        "name": "Португальский",
        "native": "Português",
        "num2words": "pt_BR",
        "thousands": [".", " "],
        "decimal": ",",
        "norm": (75, 80),
    },
    "DE": {
        "name": "Немецкий",
        "native": "Deutsch",
        "num2words": "de",
        "thousands": [".", " "],
        "decimal": ",",
        "norm": (82, 88),
    },
    "IT": {
        "name": "Итальянский",
        "native": "Italiano",
        "num2words": "it",
        "thousands": [".", " "],
        "decimal": ",",
        "norm": (85, 89),
    },
    "PL": {
        "name": "Польский",
        "native": "Polski",
        "num2words": "pl",
        "thousands": [" "],
        "decimal": ",",
        "norm": (81, 85),
    },
    "FR": {
        "name": "Французский",
        "native": "Français",
        "num2words": "fr",
        "thousands": [" "],
        "decimal": ",",
        "norm": (85, 88),
    },
    "UK": {"name": "Украинский", "native": "Українська", "num2words": "uk", "thousands": [" "], "decimal": ",", "norm": (76, 82), "extra": True},
    "TR": {"name": "Турецкий", "native": "Türkçe", "num2words": "tr", "thousands": ["."], "decimal": ",", "norm": (80, 86), "extra": True},
    "NL": {"name": "Нидерландский", "native": "Nederlands", "num2words": "nl", "thousands": ["."], "decimal": ",", "norm": (82, 88), "extra": True},
    "JA": {"name": "Японский", "native": "日本語", "num2words": "ja", "thousands": [","], "decimal": ".", "norm": (45, 55), "extra": True},
    "KO": {"name": "Корейский", "native": "한국어", "num2words": "ko", "thousands": [","], "decimal": ".", "norm": (55, 65), "extra": True},
    "ZH": {"name": "Китайский", "native": "中文", "num2words": "zh", "thousands": [","], "decimal": ".", "norm": (35, 45), "extra": True},
    "HI": {"name": "Хинди", "native": "हिन्दी", "num2words": "hi", "thousands": [","], "decimal": ".", "norm": (80, 88), "extra": True},
    "AR": {"name": "Арабский", "native": "العربية", "num2words": "ar", "thousands": [","], "decimal": ".", "norm": (75, 83), "extra": True},
}

MAIN_LANG_ORDER = ["EN", "RU", "ES", "PT", "DE", "IT", "PL", "FR"]


def api_lang(code: str) -> str:
    """Normalize internal 'PT_BR' to API 'PT'."""
    c = (code or "").strip().upper()
    return "PT" if c in ("PT_BR", "PT-BR", "BR") else c


def disk_lang(code: str) -> str:
    """Map API 'PT' to 'PT_BR' when reading/writing fl-voice_translator disk files."""
    c = (code or "").strip().upper()
    return "PT_BR" if c == "PT" else c


def list_languages(profile_norms: dict[str, tuple[int, int]] | None = None) -> list[LanguageInfo]:
    norms = profile_norms or {}
    out: list[LanguageInfo] = []
    for code, meta in LANG_CONFIG.items():
        norm = norms.get(code) or norms.get(disk_lang(code)) or meta["norm"]
        out.append(
            LanguageInfo(
                code=code,
                name=meta["name"],
                native_name=meta["native"],
                norm=(int(norm[0]), int(norm[1])),
                is_extra=bool(meta.get("extra", False)),
            )
        )
    return out


# ------------------------------------------------------- Emotion tags (22.10) ---

SQ_TAG_RE = re.compile(r"\[[^\[\]\n]{1,160}\]")
PAIR_TAG_RE = re.compile(r"</?([a-z][a-z_-]{1,20})>|<\|speaker:\d+\|>")
TAG_RE = re.compile(SQ_TAG_RE.pattern + "|" + PAIR_TAG_RE.pattern)

SOUND_TAG_RE = re.compile(
    r"^\[\s*(?:(?:short|long|small|brief|deep|shaky|soft|quiet|heavy|sharp)\s+)*"
    r"(?:pause|beat|silence|breath|breathes|breathing|inhale|exhale|sigh|sighs|laugh|laughs|"
    r"laughter|chuckle|chuckles|cough|gasp|gasps|sniff|clears throat)\s*\]$",
    re.IGNORECASE,
)


def strip_tags(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", TAG_RE.sub(" ", text or "")).strip()


def has_tags(text: str) -> bool:
    return bool(TAG_RE.search(text or ""))


def tags_problem(src: str, tr: str) -> str:
    """Check if translation preserved all emotion and paired tags from source."""
    if not has_tags(src) and not has_tags(tr):
        return ""
    s_sq = [t.lower() for t in SQ_TAG_RE.findall(src or "")]
    t_sq = [t.lower() for t in SQ_TAG_RE.findall(tr or "")]
    out: list[str] = []
    lost = [t for t in s_sq if t not in t_sq]
    extra = [t for t in t_sq if t not in s_sq]
    if lost:
        out.append("нет " + ", ".join(lost[:3]))
    if extra:
        out.append("тег переведён или лишний: " + ", ".join(extra[:3]))
    if not lost and not extra and s_sq != t_sq:
        out.append("теги в квадратных скобках идут не в том порядке")
    s_pair = sorted(m.group(0) for m in PAIR_TAG_RE.finditer(src or ""))
    t_pair = sorted(m.group(0) for m in PAIR_TAG_RE.finditer(tr or ""))
    if s_pair != t_pair:
        names = sorted({f"<{m.group(1)}>" if m.group(1) else m.group(0) for m in PAIR_TAG_RE.finditer(src or "")})
        out.append("парные теги не совпадают (" + (", ".join(names) or "нет в оригинале") + ")")
    return "; ".join(out)


def mood_tags(text: str) -> list[str]:
    return [t for t in SQ_TAG_RE.findall(text or "") if not SOUND_TAG_RE.match(t)]


def carry_plan(texts: list[str]) -> dict[int, str]:
    """Map 1-based paragraph index -> carried mood tag from previous paragraphs."""
    out: dict[int, str] = {}
    last: str | None = None
    for i, t in enumerate(texts, 1):
        m = SQ_TAG_RE.match((t or "").lstrip())
        starts_with_mood = bool(m) and not SOUND_TAG_RE.match(m.group(0))
        if not starts_with_mood and last:
            out[i] = last
        own = mood_tags(t)
        if own:
            last = own[-1]
    return out


def with_carry(text: str, tag: str | None) -> str:
    return f"{tag} {text}" if tag else text


# ------------------------------------------------------- Spoken length & checks ---

NUM_RE = re.compile(r"\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def _parse_number(token: str, lang: str) -> int | float | None:
    meta = LANG_CONFIG.get(api_lang(lang), LANG_CONFIG["EN"])
    t = token.replace("\u00a0", " ").replace("\u202f", " ")
    for sep in meta["thousands"]:
        t = re.sub(rf"(?<=\d){re.escape(sep)}(?=\d{{3}}(\D|$))", "", t)
    t = t.replace(" ", "")
    if meta["decimal"] != ".":
        t = t.replace(meta["decimal"], ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return int(v) if v.is_integer() else v


def spoken_len(text: str, lang: str) -> int:
    """Character length with numbers expanded to spoken words and emotion tags stripped."""
    norm_lang = api_lang(lang)
    code = LANG_CONFIG.get(norm_lang, LANG_CONFIG["EN"])["num2words"]
    cleaned = strip_tags(text)

    def repl(m: re.Match) -> str:
        v = _parse_number(m.group(0), norm_lang)
        if v is None:
            return m.group(0)
        try:
            if norm_lang == "EN" and isinstance(v, int) and 1000 <= v <= 1999 and "," not in m.group(0):
                return num2words(v, lang=code, to="year")
            return num2words(v, lang=code)
        except Exception:
            return m.group(0)

    return len(re.sub(r"\s+", " ", NUM_RE.sub(repl, cleaned)).strip())


def evaluate_paragraph(
    src_text: str,
    tr_text: str,
    source_lang: str,
    target_lang: str,
    norm: tuple[int, int],
    lower_tolerance_pp: int = 6,
    short_chars: int = 150,
    short_tolerance: int = 5,
) -> tuple[float, bool, bool, str | None, int]:
    """Return (length_pct, in_norm, tags_ok, tags_problem_str, chars_delta)."""
    sl = max(1, spoken_len(src_text, source_lang))
    tl = spoken_len(tr_text, target_lang)
    pct = round(tl / sl * 100, 1)

    lo, hi = norm
    if sl < short_chars:
        lo, hi = lo - short_tolerance, min(hi + short_tolerance, max(99, hi))
    min_chars = int(sl * lo / 100) + 1
    max_chars = int(sl * hi / 100)
    min_accept = int(min_chars - sl * lower_tolerance_pp / 100)

    tp = tags_problem(src_text, tr_text)
    t_ok = not bool(tp)

    if tl > max_chars:
        chars_delta = tl - max_chars
        in_norm = False
    elif tl < min_accept:
        chars_delta = tl - min_chars
        in_norm = False
    else:
        chars_delta = 0
        in_norm = t_ok

    return pct, in_norm, t_ok, (tp or None), chars_delta


# ------------------------------------------------------- Document & blocks ---

MANUAL_NUM_RE = re.compile(r"^\s*(\d{1,4})[.)](\s+)")
SHORT_LIMIT = 30
LANG_TAIL_RE = re.compile(r"_(PT_BR|PT|RU|ES|DE|IT|PL|FR|EN)$", re.IGNORECASE)
KEY_RE = re.compile(r"^\s*##\s+(H?\d+)\s*$", re.MULTILINE)


def _is_numbered(p) -> bool:
    p_pr = p._p.pPr
    return p_pr is not None and p_pr.numPr is not None


def split_doc(doc: Document) -> list[dict]:
    """Extract numbered paragraphs and headings in order."""
    paras = [p for p in doc.paragraphs if p.text.strip()]
    word_list = any(_is_numbered(p) for p in paras)
    out: list[dict] = []
    h, n = 0, 0
    for p in paras:
        text, prefix = p.text.strip(), ""
        numbered = _is_numbered(p)
        if not word_list:
            m = MANUAL_NUM_RE.match(p.text)
            if m and ("\t" in m.group(2) or int(m.group(1)) == n + 1) and p.text[m.end() :].strip():
                numbered = True
                prefix = p.text[: m.end()].lstrip()
                text = p.text[m.end() :].strip()
        if numbered:
            n += 1
            out.append({"p": p, "key": str(n), "heading": False, "text": text, "prefix": prefix})
        else:
            h += 1
            out.append({"p": p, "key": f"H{h}", "heading": True, "text": text, "prefix": ""})
    return out


def short_name(stem: str, limit: int = SHORT_LIMIT) -> str:
    s = re.sub(r"[^\w-]+", "_", stem.strip())
    s = re.sub(r"_{2,}", "_", s).strip("_-")
    s = re.sub(r"[_-]+(ENG|eng|Eng)$", "", s)
    m = LANG_TAIL_RE.search(s)
    lang = m.group(1).upper() if m else ""
    base = s[: m.start()] if m else s
    if len(base) > limit + 6:
        cut = base[: limit + 1]
        cut = cut[: cut.rfind("_")] if "_" in cut[1:] else base[:limit]
        base = cut.strip("_-") or base[:limit]
    base = base or "script"
    return f"{base}_{lang}" if lang and lang != "EN" else base


def detect_source_lang(text: str) -> str:
    """Detect source language of a script from its text."""
    sample = strip_tags(text)[:4000]
    if re.search(r"[А-Яа-яЁё]", sample):
        if re.search(r"[іїєґІЇЄҐ]", sample):
            return "UK"
        return "RU"
    low = f" {sample.lower()} "
    scores = {
        "EN": sum(low.count(w) for w in (" the ", " and ", " was ", " with ", " that ", " from ")),
        "DE": sum(low.count(w) for w in (" der ", " die ", " und ", " في ", " nicht ", " mit ", " ein ")),
        "ES": sum(low.count(w) for w in (" el ", " la ", " los ", " las ", " una ", " por ", " para ")),
        "PT": sum(low.count(w) for w in (" não ", " uma ", " com ", " para ", " dos ", " das ", " pelo ")),
        "FR": sum(low.count(w) for w in (" les ", " des ", " une ", " dans ", " pour ", " avec ", " est ")),
        "IT": sum(low.count(w) for w in (" gli ", " della ", " nella ", " sono ", " con ", " non ", " che ")),
        "PL": sum(low.count(w) for w in (" się ", " nie ", " jest ", " oraz ", " przez ", " jak ")),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 1 else "EN"


def parse_blocks(raw: str) -> dict[str, str]:
    raw = re.sub(r"^```[a-z]*\s*$", "", raw, flags=re.MULTILINE)
    parts = KEY_RE.split(raw)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        text = re.sub(r"\s+", " ", parts[i + 1]).strip()
        if text:
            out[parts[i]] = text
    return out


def load_translation(proj: Path, lang: str) -> dict[str, str]:
    for candidate in (disk_lang(lang), api_lang(lang)):
        f = proj / f"{candidate}.md"
        if f.exists():
            return parse_blocks(f.read_text(encoding="utf-8"))
    return {}


def save_translation(proj: Path, lang: str, blocks: dict[str, str]) -> None:
    d_lang = disk_lang(lang)
    merged = load_translation(proj, d_lang)
    merged.update(blocks)
    src = json.loads((proj / "source.json").read_text(encoding="utf-8"))
    lines: list[str] = []
    for it in src["items"]:
        if it["key"] in merged:
            lines += [f"## {it['key']}", merged[it["key"]], ""]
    target = proj / f"{d_lang}.md"
    tmp = proj / f"{d_lang}.md.{os.getpid()}.tmp"
    tmp.write_text("\n".join(lines), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, target)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1)


def build_docx(proj: Path, output_dir: Path, lang: str) -> Path | None:
    src = json.loads((proj / "source.json").read_text(encoding="utf-8"))
    docx_path = Path(src.get("docx", ""))
    if not docx_path.exists():
        return None
    tr = load_translation(proj, lang)
    doc = Document(str(docx_path))
    for e in split_doc(doc):
        p, new = e["p"], tr.get(e["key"])
        if new is None:
            continue
        new = e["prefix"] + new
        if p.runs:
            p.runs[0].text = new
            for r in p.runs[1:]:
                r._r.getparent().remove(r._r)
        else:
            p.add_run(new)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{src['name']}_{disk_lang(lang)}.docx"
    doc.save(str(out))
    return out
