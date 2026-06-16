import asyncio
import logging

from aiogram import Bot

from app.config import settings
from app.handlers import dp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


async def main() -> None:
    if not settings.bot_token or settings.bot_token == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN env variable")

    bot = Bot(token=settings.bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
