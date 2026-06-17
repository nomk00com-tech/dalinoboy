"""
Entry point — starts the Telegram bot + APScheduler.

Usage:
    python main.py
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

import db
import handlers
import scheduler as sched

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set in .env")

    await db.init_db()

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers.router)

    scheduler = sched.start_scheduler(bot)

    log.info("Bot starting…")
    try:
        # drop_pending_updates: ignore the backlog of taps queued while the bot was
        # offline (otherwise we get harmless "query is too old" errors on startup).
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
