import asyncio
import logging
import os

from aiogram import Bot
from aiohttp import web

from app.config import settings
from app.handlers import dp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

async def start_webserver():
    # Render предоставляет порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    
    # Простой endpoint для проверки работоспособности
    async def health_check(request):
        return web.Response(text="Bot is running")
    
    app.router.add_get("/", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on 0.0.0.0:{port}")
    
    # Бесконечный цикл, чтобы задача не завершалась
    while True:
        await asyncio.sleep(3600)

async def main() -> None:
    if not settings.bot_token or settings.bot_token == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN env variable")

    bot = Bot(token=settings.bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запускаем polling бота и веб-сервер параллельно
    await asyncio.gather(
        dp.start_polling(bot),
        start_webserver()
    )

if __name__ == "__main__":
    asyncio.run(main())