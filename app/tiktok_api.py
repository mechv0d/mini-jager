import logging
import aiohttp
from aiohttp import ClientTimeout
from app.config import settings

logger = logging.getLogger(__name__)

class TikTokAPIError(Exception):
    """Кастомное исключение для ошибок TikWM API"""
    pass

async def fetch_tiktok_media_data(url: str) -> dict:
    """
    Делает запрос к tikwmapi.com и возвращает готовые данные о медиа.
    API сам разрешает короткие ссылки (https://vt.tiktok.com/...) 
    и обходит Cloudflare 403 Forbidden.
    """
    api_url = "https://api.tikwmapi.com/"
    
    params = {"url": url}
    if settings.tikwm_hd_quality:
        params["hd"] = "1"
        
    headers = {
        "x-tikwmapi-key": settings.tikwm_api_key
    }
    
    timeout = ClientTimeout(total=settings.download_timeout)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(api_url, params=params, headers=headers) as response:
                if response.status == 429:
                    raise TikTokAPIError("Превышен лимит запросов API (Rate limit 429).")
                if response.status != 200:
                    raise TikTokAPIError(f"API вернул статус {response.status}")
                    
                data = await response.json()
                
                # TikWM возвращает code=0 при успехе
                if data.get("code") != 0:
                    msg = data.get("msg", "Неизвестная ошибка API")
                    raise TikTokAPIError(f"Ошибка API: {msg}")
                    
                return data.get("data", {})
                
        except aiohttp.ClientError as e:
            logger.exception(f"Сетевая ошибка при запросе к TikWM API: {e}")
            raise TikTokAPIError(f"Не удалось подключиться к API: {str(e)}")