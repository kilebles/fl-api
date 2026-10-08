# fl-api

Generation core for Google Flow (Veo video + image models): Dolphin Anty accounts, reCAPTCHA browser sessions, rotation, and ready-made batch modes. Extracted from `../flow-api` (Telegram bot) on 2026-10-07 without the bot, prompt generation, Yandex Disk, stock images and WARP. A web API for the site will wrap `JobQueue`.

## Tech Stack

- **Python 3.12+**, **uv**
- **httpx** (async, http2), **playwright** (CDP to Dolphin Anty), **pydantic / pydantic-settings**, **loguru**, **openpyxl** (xlsx input)

## Layout

```
src/
├── core/        settings.py, errors.py, logger.py (loguru + flow-stat sink)
├── accounts/    AccountInfo, ProxyInfo, AccountRepository (accounts/account_N/*.txt)
├── dolphin/     manager.py (DolphinAntyManager: start/rotate/proxies), local_api.py, public_api.py,
│                cooldowns.py (per-account pauses: cooldown / image quota / video denied)
├── flow/        HTTP clients for aisandbox-pa.googleapis.com: video.py, image.py, polling.py,
│                downloader.py, http.py (headers, error reasons), credentials.py, models.py,
│                browser/recaptcha.py (RecaptchaTokenProducer), human_behavior.py, timing.py
├── engine/      session.py (AccountSession — one browser session + the whole recovery policy),
│                scheduler.py (BatchScheduler — image/video queues, workers), chain.py (f2v), frames.py (ffmpeg)
└── jobs/        modes.py (Mode, Strategy, MODES), models.py (JobSpec, JobRow, JobState, JobStatus),
                 storage.py (JobOutput — output folder = job state), runner.py (JobRunner — passes),
                 queue.py (JobQueue — one job at a time), excel.py (xlsx → rows)
scripts/         run_job.py (CLI), check_session.py (session smoke test), probe_image_model.py
tests/           fakes + tests for scheduler, runner, queue, session recovery
```

## Flow of a job

```
JobQueue.submit(JobSpec) → JobRunner.run → passes:
   JobOutput.missing() → BatchScheduler(sessions).run(work_items)   (or run_chain for f2v)
      worker per AccountSession: pick video (image ready) else image → session.make_image / submit_video
      → finish_video (poll + download) in the background → {name}.mp4 / {name}.jpg / {name}.skip
   no progress in a pass → wait for the earliest cooldown if every account is paused, else STOPPED
```

- **Strategy.PARALLEL**: one `AccountSession` per account (single-account `DolphinAntyManager`); rotation restarts the same profile, an account on cooldown just stops working.
- **Strategy.SEQUENTIAL**: one session over a manager with all accounts; rotation moves to the next account. f2v always runs this way.
- Jobs run one at a time — they all drive the same Dolphin profiles.

## Modes (`src/jobs/modes.py`)

| Mode | Rows | Result | Video model |
|---|---|---|---|
| `t2v` | prompt | mp4 | `VIDEO_MODEL_KEY` |
| `flash_t2v` | prompt | mp4 | `FLASH_T2V_MODEL_KEY` (abra_t2v_10s) |
| `img` | image_prompt | jpg | — (`IMAGE_MODEL_NAME`) |
| `i2v` | prompt + image_prompt, or `static_image` for all rows | jpg + mp4 | `I2V_MODEL_KEY` |
| `flash_i2v` | prompt + image_prompt | jpg + mp4 | `FLASH_I2V_MODEL_KEY` (abra_r2v_10s) |
| `i2i2v` | prompt + image_prompt + `avatar` (uploaded once per project, passed as `imageInputs`) | jpg + mp4 | `I2V_MODEL_KEY` |
| `f2v` | prompt, in order; clip N starts from the last frame of the last good clip | mp4 (+ frames/) | first clip `FLASH_T2V_MODEL_KEY`, then `F2V_MODEL_KEY` |

## Recovery policy (`AccountSession`)

- **401**: re-sign-in to labs.google once, then rotate.
- **403**: pause 7–14s, then 30–60s; 3rd in a row → rotate; 2 such rotations on the same proxy → account cooldown (5m doubling, max 2h). `PUBLIC_ERROR_MODEL_ACCESS_DENIED` = plan lacks the model: pauses only videos (`mark_video_denied`, 30m doubling) or only images; no rotation.
- **429**: `PER_MODEL_DAILY_QUOTA_REACHED` → video: account cooldown (max 4h) + rotation; image: image-only cooldown (`ImageQuotaError`), the account keeps making videos. `TOO_MUCH_TRAFFIC` → pause 2/5/10 min, no rotation. Others → 7/30/90s pause, 4th in a row rotates.
- **400**: permanent skip (`.skip`).
- Polling: `IP_PROHIBITED` → rotate; server `TIMED_OUT` twice on different rows → rotate, twice on the same row → skip.
- reCAPTCHA token timeout: wait once, then restart the browser. `RecaptchaInterfaceError`, `DiskFullError`, `DolphinNotRunningError` stop the job (`FATAL_ERRORS`).
- Every `ROTATE_EVERY_GENERATIONS` videos the session rotates in the background.

## Flow frontend notes

Flow lives on `flow.google.com` (since 2026-09-09). The API token (`ya29…`) comes from the legacy next-auth session `labs.google/fx/api/auth/session`; the producer re-signs in automatically and refreshes every `LABS_SESSION_REFRESH_INTERVAL`. Project pages: `flow.google.com/project/{id}`, persisted per account in `project_id.txt`. Flow wraps `grecaptcha.execute`; the init script in `recaptcha.py` captures the original. When images return 404 "Requested entity was not found", the image model name was retired: run `scripts/probe_image_model.py` and set `IMAGE_MODEL_NAME`.

## Accounts

```
accounts/account_N/
├── dolphin_id.txt
├── proxies.txt         # host:port:user:password per line
├── dead_proxies.txt    # maintained automatically (profiles that failed to start on a proxy)
└── project_id.txt      # written automatically
```

## Monitoring

`MONITOR_URL` enables the flow-stat sink (`../flow-stat`); events carry `event_type`, `account`, `proxy` via `logger.bind`. One `success` event per saved file.

## Usage

```bash
uv sync
uv run python scripts/run_job.py t2v path/to/rome_179.xlsx [--strategy sequential]
uv run python scripts/check_session.py account_1 [--generate]
uv run pytest
```

## Code Style

- Type hints everywhere, English docstrings on public classes/methods, no comments unless necessary
- loguru for logging, async/await for I/O
- Errors of a running job go to logs and flow-stat; callers see only the job state and progress
