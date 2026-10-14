"""
Import prompt templates saved by the hdoc-prompts bot (data/prompts/*.json) into the database.

Usage:
    uv run python scripts/import_prompt_templates.py ../hdoc-prompts/data/prompts

Re-running updates templates with the same id instead of duplicating them.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.db import Database
from src.core.settings import get_settings
from src.prompts.llm import LLMClient
from src.prompts.models import I2VPrompt
from src.prompts.service import PromptService


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import hdoc-prompts templates")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.database_url)
    await db.init()
    service = PromptService(db, LLMClient("", ""), settings.prompt_template_model)
    count = 0
    for path in sorted(args.folder.glob("*.json")):
        try:
            saved = json.loads(path.read_text())
            template = I2VPrompt.model_validate(saved["data"])
        except (ValueError, KeyError) as e:
            print(f"skipped {path.name}: {type(e).__name__}")
            continue
        if saved.get("model_type") != "i2v":
            continue
        await service.save_template(template, template_id=saved["id"])
        count += 1
    await db.close()
    print(f"imported {count} templates")


if __name__ == "__main__":
    asyncio.run(main())
