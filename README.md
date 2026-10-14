# fl-api

Generation core for Google Flow (Veo): Dolphin Anty accounts, reCAPTCHA sessions, rotation and batch modes
(`t2v`, `flash_t2v`, `img`, `i2v`, `flash_i2v`, `i2i2v`, `f2v`). See `CLAUDE.md` for the architecture.

```bash
cp .env.example .env            # set ANTY_TOKEN
uv sync
uv run python scripts/run_job.py i2v i2v_rome_220.xlsx
uv run pytest
```

Web API (results in `storage/` by default; `STORAGE=s3` for R2, `docker compose up -d` for a local S3):

```bash
uv run python scripts/serve.py          # http://127.0.0.1:8000/docs
```
