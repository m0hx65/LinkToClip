from __future__ import annotations

import asyncio
import logging

from aiohttp.web_runner import AppRunner

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers.download import router as download_router
from bot.health_server import start_if_configured
from bot.middlewares import SettingsMiddleware
from utils.config import load_settings
from utils.logging_setup import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware.register(SettingsMiddleware(settings))
    dp.include_router(download_router)

    logger.info("Bot starting")
    http_runner: AppRunner | None = await start_if_configured()
    try:
        await dp.start_polling(bot)
    finally:
        if http_runner is not None:
            await http_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
