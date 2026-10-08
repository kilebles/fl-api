# fl-api

Generation core for Google Flow (Veo): Dolphin Anty accounts, reCAPTCHA sessions, rotation and batch modes
(`t2v`, `flash_t2v`, `img`, `i2v`, `flash_i2v`, `i2i2v`, `f2v`). See `CLAUDE.md` for the architecture.

```bash
cp .env.example .env            # set ANTY_TOKEN
uv sync
uv run python scripts/run_job.py i2v i2v_rome_220.xlsx
uv run pytest
```

Programmatic use (what a web API wraps):

```python
from src.accounts.repository import AccountRepository
from src.core.settings import get_settings
from src.jobs.models import JobRow, JobSpec
from src.jobs.modes import Mode
from src.jobs.queue import JobQueue
from src.jobs.runner import JobRunner

settings = get_settings()
queue = JobQueue(JobRunner(settings, AccountRepository(settings.accounts_dir)))
queue.start()                                   # inside the running event loop
status = queue.submit(JobSpec(mode=Mode.T2V, rows=[JobRow(name="1", prompt="...")], output_dir=settings.output_dir / "job1"))
queue.get(status.id)                            # state + progress
queue.cancel(status.id)
```
