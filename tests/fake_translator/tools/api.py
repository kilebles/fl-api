"""A small stand-in for fl-voice_translator's tools/api.py, for CI (the translator repo is private).

It speaks the same JSON protocol and writes the same files (work/<script>/source.json, <LANG>.md, <LANG>.stats.json,
queue/NNNN.json), with a simplified length check. Run the voice tests against the real translator with
FL_TRANSLATOR_REPO=/path/to/fl-voice_translator.
"""

import json
import re
import sys
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parent.parent
WORK, QUEUE, OUTPUT = ROOT / "work", ROOT / "queue", ROOT / "output"
CFG = json.loads((ROOT / "tools" / "config.json").read_text(encoding="utf-8"))
LANGS = ["EN"] + CFG["language_order"]


def fail(code, message):
    print(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False))
    sys.exit(1)


def lang_code(l):
    l = (l or "").upper()
    return "PT_BR" if l in ("PT", "PT-BR") else l


def items_of(docx):
    out, h, n = [], 0, 0
    for p in Document(str(docx)).paragraphs:
        t = p.text.strip()
        if not t:
            continue
        m = re.match(r"^(\d{1,4})[.)]\t(.+)$", t)
        if m:
            n += 1
            out.append({"key": str(n), "heading": False, "text": m.group(2).strip()})
        else:
            h += 1
            out.append({"key": f"H{h}", "heading": True, "text": t})
    return out


def source(slug):
    f = WORK / slug / "source.json"
    if not f.exists():
        fail("script_not_found", f"Сценарий {slug} не найден")
    return json.loads(f.read_text(encoding="utf-8"))


PROFILE = None


def ranges():
    out = {k: (v["min"], v["max"]) for k, v in CFG["languages"].items()}
    try:
        data = json.loads((ROOT / "tools" / "voices.json").read_text(encoding="utf-8"))
        prof = next((p for p in data["profiles"] if p["id"] == (PROFILE or data.get("active"))), {})
        out.update({k: tuple(v) for k, v in (prof.get("percents") or {}).items()})
    except (OSError, ValueError, KeyError):
        pass
    return out


def pair_range(lang, src):
    r = ranges()
    lo, hi = r[lang]
    if src == "EN":
        return lo, hi
    mid = sum(r[src]) / 2
    if lang == "EN":
        lo, hi = 95.5, 98.5
    return round(lo * 97 / mid), round(hi * 97 / mid)


def load_tr(slug, lang):
    f = WORK / slug / f"{lang}.md"
    if not f.exists():
        return {}
    parts = re.split(r"^##\s+(H?\d+)\s*$", f.read_text(encoding="utf-8"), flags=re.M)
    return {parts[i]: " ".join(parts[i + 1].split()) for i in range(1, len(parts) - 1, 2) if parts[i + 1].strip()}


def save_tr(slug, lang, blocks):
    merged = dict(load_tr(slug, lang), **blocks)
    lines = []
    for it in source(slug)["items"]:
        if it["key"] in merged:
            lines += [f"## {it['key']}", merged[it["key"]], ""]
    (WORK / slug / f"{lang}.md").write_text("\n".join(lines), encoding="utf-8")


def stat(it, text, lang, src):
    sl, tl = max(1, len(it["text"])), len(text or "")
    lo, hi = pair_range(lang, src)
    pct = round(tl / sl * 100, 1)
    problem = None if text else "нет перевода"
    if text and pct > hi:
        problem = "длиннее"
    elif text and pct < lo - 6:
        problem = "короче"
    tags = None
    if re.findall(r"\[[^\]]+\]", it["text"]) != re.findall(r"\[[^\]]+\]", text or "") and text:
        tags = problem = "теги озвучки"
    delta = int(tl - sl * hi / 100) if pct > hi else (int(tl - sl * lo / 100) if text and pct < lo - 6 else 0)
    return {"pct": pct, "src": sl, "tr": tl, "lo": lo, "hi": hi, "in_norm": not problem, "problem": problem,
            "delta": delta, "tags_problem": tags}


def write_stats(slug, lang):
    src = source(slug)
    s_lang = src.get("source_lang", "EN")
    tr = load_tr(slug, lang)
    paras = {it["key"]: stat(it, tr.get(it["key"]), lang, s_lang) for it in src["items"] if not it["heading"]}
    done = [k for k in paras if tr.get(k)]
    total = round(sum(paras[k]["tr"] for k in done) / sum(paras[k]["src"] for k in done) * 100, 1) if done else None
    data = {"lang": lang, "source_lang": s_lang, "range": list(pair_range(lang, s_lang)), "total": total,
            "translated": len(done), "in_norm": sum(1 for v in paras.values() if v["in_norm"]), "paragraphs": paras}
    (WORK / slug / f"{lang}.stats.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / f"{slug}_{lang}.docx").write_bytes(b"docx")
    return data


def new_job(job):
    QUEUE.mkdir(exist_ok=True)
    n = max([int(f.name[:4]) for f in QUEUE.glob("[0-9][0-9][0-9][0-9].json")] or [0]) + 1
    job["id"] = n
    (QUEUE / f"{n:04d}.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    return job


def get_job(n):
    f = QUEUE / f"{int(n):04d}.json"
    if not f.exists():
        fail("job_not_found", f"Задачи #{n} нет")
    return json.loads(f.read_text(encoding="utf-8"))


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    opts = {args[i][2:]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith("--")}
    pos = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    global PROFILE
    PROFILE = opts.get("profile")
    if cmd == "prepare":
        docx = Path(pos[0])
        items = items_of(docx)
        if not any(not i["heading"] for i in items):
            fail("invalid_docx", "В документе нет нумерованных абзацев")
        slug = re.sub(r"_(ENG|EN)$", "", docx.stem)
        f = WORK / slug / "source.json"
        old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        text = " ".join(i["text"] for i in items)
        lang = lang_code(opts.get("source-lang")) or old.get("source_lang") or ("RU" if re.search("[а-я]", text) else "EN")
        f.parent.mkdir(parents=True, exist_ok=True)
        data = {"docx": str(docx.resolve()), "name": slug, "source_lang": lang, "items": items,
                "title": opts.get("title") or old.get("title")}
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out = {"script_id": slug, "source_lang": lang, "title": data["title"], "paragraphs": len(items)}
    elif cmd == "submit":
        spec = json.loads(sys.stdin.read())
        src = source(spec["script"])
        s_lang = src.get("source_lang", "EN")
        langs = [l for l in map(lang_code, spec.get("langs") or []) if l != s_lang] or [l for l in LANGS if l != s_lang]
        vm = {"s2.1": "s2.1-pro"}.get(spec.get("voice_model"), spec.get("voice_model"))
        extra = {"source": "web", "web": spec.get("web") or {}, "voices": spec.get("voices"), "voice_model": vm,
                 "carry_mood": bool(spec.get("carry_mood"))}
        base = {"profile": spec.get("profile") or "p1", "status": "pending", "added": "24.10 10:00", "results": {},
                "chat_id": None, **extra}
        jobs = []
        if spec["mode"] in ("all", "translate"):
            groups = [[l] for l in langs] if spec.get("split") and len(langs) > 1 else [langs]
            for g in groups:
                j = dict(base, kind="translate", docx=src["docx"], name=Path(src["docx"]).name, langs=g,
                         pipeline=spec["mode"] == "all", engine=spec.get("engine") or "agy")
                if spec.get("engine") == "agy" and spec.get("agy_model"):
                    j["agy_model"] = spec["agy_model"]
                jobs.append(new_job(j))
        elif spec["mode"] == "voice":
            items = [{"docx": src["docx"], "lang": s_lang, "voice": (spec.get("voices") or {}).get(s_lang), "source": True}]
            items += [{"docx": f"output/{spec['script']}_{l}.docx", "lang": l, "voice": (spec.get("voices") or {}).get(l)}
                      for l in langs]
            jobs.append(new_job(dict(base, kind="voice", items=items, langs=[i["lang"] for i in items], files=[],
                                     name=Path(src["docx"]).name, pipeline=False)))
        else:
            jobs.append(new_job(dict(base, kind="mix", script=spec["script"], langs=[], files=[], pipeline=False)))
        out = {"jobs": jobs}
    elif cmd in ("stop", "drop", "resume"):
        job = get_job(pos[0])
        f = QUEUE / f"{job['id']:04d}.json"
        if cmd == "resume":
            job["status"] = "pending"
            f.write_text(json.dumps(job), encoding="utf-8")
            out = {"id": job["id"], "result": "pending"}
        elif job["status"] == "running":
            (QUEUE / f"{job['id']:04d}.cmd").write_text(json.dumps({"cmd": cmd}), encoding="utf-8")
            out = {"id": job["id"], "result": "requested"}
        elif cmd == "drop":
            f.unlink()
            out = {"id": job["id"], "result": "dropped"}
        else:
            if job["status"] == "pending":
                job["status"] = "stopped"
                f.write_text(json.dumps(job), encoding="utf-8")
            out = {"id": job["id"], "result": "stopped"}
    elif cmd == "revoice":
        job = get_job(pos[0])
        voices = json.loads(sys.stdin.read() or "{}").get("voices") or {}
        items = [dict(i, voice=voices.get(i["lang"], i["voice"])) for i in job["items"] if i["lang"] in (job.get("skipped") or {})]
        new = dict(job, items=items, status="pending", results={}, skipped={}, root=job.get("root") or job["id"],
                   parent=job["id"])
        out = {"job": new_job(new)}
    elif cmd == "stats":
        out = write_stats(pos[0], lang_code(pos[1]))
    elif cmd in ("measure", "set-paragraph"):
        slug, lang, key = pos[0], lang_code(pos[1]), pos[2]
        src = source(slug)
        it = next((i for i in src["items"] if i["key"] == key and not i["heading"]), None)
        if not it:
            fail("paragraph_not_found", f"Абзац #{key} не найден")
        text = " ".join(sys.stdin.read().split())
        if cmd == "set-paragraph":
            save_tr(slug, lang, {key: text})
            write_stats(slug, lang)
        out = stat(it, text, lang, src.get("source_lang", "EN"))
    elif cmd == "mix-one":
        out = {"langs": [], "slot_sec": 0, "speeds": {}}
    elif cmd == "retranslate":
        fail("limit", "Лимит подписки исчерпан, сброс около 24.10 18:00")
    else:
        fail("bad_request", cmd)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
