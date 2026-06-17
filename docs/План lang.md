Да, хорошая задача. Я бы вынес это не в `.env`, а в отдельный файл **`app/lang.py`**: так можно хранить шаблоны с `{url}`, `{seconds}`, `{limit_mb}` и не плодить огромный `.env`.

## Цель

Сделать так, чтобы весь пользовательский текст редактировался в одном месте:

```text
app/lang.py
```

А в коде осталось только:

```python
from app.lang import t
```

и вызовы вида:

```python
await message.answer(t.loading_started)
await message.answer(t.error.format(error=error_text))
```

Логи `logger.info`, `logger.warning`, `logger.exception` **не трогаем**. Они остаются техническими консольными сообщениями.

---

## Что выносить

### 1. `handlers.py`

Там сейчас больше всего пользовательского текста: стартовое сообщение, “Загрузка началась”, inline-заголовки, описания, callback-ответы, ошибки. Например, в `start_handler`, `message_handler`, `inline_query_handler`, `inline_loading_keyboard`, `process_inline_video_job` есть строки, которые видит пользователь. 

Выносить:

```python
"Загрузка выполняется"
"Привет! Отправь ссылку..."
"Загрузка началась"
"Ошибка: {error_text}"
"Вставь ссылку TikTok"
"Пришли ссылку на TikTok-видео."
"Отправить видео"
"Видео готово"
"Фото из TikTok-альбома"
"Скачать TikTok"
"Видео появится после загрузки..."
"Медиа ещё загружается..."
"Фотоальбом готов..."
```

И после последних правок ещё:

```python
"Audio"
"Photo {index}"
"Video {index}"
"Не удалось подготовить фото"
```

### 2. `telegram_service.py`

Там пользовательские ошибки и сообщения, которые возвращаются через `public_error_message()` или `PublicBotError`: таймауты, отсутствие `DUMP_CHAT_ID`, ошибки Telegram file_id, неизвестный тип медиа. 

Выносить:

```python
"таймаут {seconds} секунд"
"внутренняя ошибка: ..."
"Для inline-режима нужен служебный чат/канал..."
"у видео нет файла"
"у фотоальбома нет файлов"
"ошибка отправки видео..."
"Telegram не вернул video.file_id"
"Telegram не вернул photo.file_id"
"неизвестный тип медиа {kind}"
```

### 3. `download_service.py`

Там пользовательские ошибки скачивания: IP заблокирован, proxy не работает, TikTok вернул пустой JSON, невалидная ссылка, слишком большое фото/видео и так далее. 

Выносить:

```python
"TikTok заблокировал текущий IP-адрес..."
"не удалось подключиться к TIKTOK_PROXY..."
"TikTok API вернул пустой или невалидный ответ..."
"таймаут: ..."
"внутренняя ошибка скачивания: ..."
"невалидная ссылка: поддерживаются только ссылки TikTok"
"фото слишком большое..."
"видео слишком большое..."
"ссылка ведёт на фотоальбом, а ожидалось видео"
```

Не выносить:

```python
logger.info(...)
logger.warning(...)
logger.exception(...)
```

---

## Предлагаемая структура `app/lang.py`

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Lang:
    # Common
    error: str = "Ошибка: {error}"
    internal_error: str = "внутренняя ошибка: {type}: {message}"
    timeout_seconds: str = "таймаут {seconds} секунд"

    # Start / messages
    start: str = (
        "Привет! Отправь ссылку на TikTok-видео или фотоальбом, "
        "и я отправлю медиа сюда.\n\n"
        "Также можно использовать inline-режим: @{bot_username} https://www.tiktok.com/..."
    )
    loading_started: str = "Загрузка началась"

    # Inline common
    inline_loading_button: str = "Загрузка выполняется"
    inline_loading_callback: str = "Медиа ещё загружается. Сообщение обновится автоматически."
    inline_help_title: str = "Вставь ссылку TikTok"
    inline_help_description: str = "Пример: @{bot_username} https://www.tiktok.com/..."
    inline_help_message: str = "Пришли ссылку на TikTok-видео или фотоальбом."

    # Inline video
    inline_send_video_title: str = "Отправить видео"
    inline_video_ready: str = "Видео готово"
    inline_download_title: str = "Скачать TikTok"
    inline_download_description: str = "Видео появится после загрузки"
    inline_download_message: str = "Загрузка началась. Медиа появится здесь автоматически."

    # Inline photo/audio
    inline_photo_title: str = "Photo {index}"
    inline_video_slide_title: str = "Video {index}"
    inline_audio_title: str = "Audio"
    inline_photo_description: str = "Фото из TikTok-альбома"
    inline_album_ready_retry: str = "Фотоальбом готов. Для выбора всех фото повторите inline-запрос."
    inline_photo_prepare_failed_title: str = "Не удалось подготовить фото"
    inline_photo_prepare_failed_description: str = "{error}"

    # Captions
    photo_caption: str = "Original: {url}"

    # Download errors
    invalid_tiktok_url: str = "невалидная ссылка: поддерживаются только ссылки TikTok"
    ip_blocked: str = (
        "TikTok заблокировал текущий IP-адрес. "
        "Проверь TIKTOK_PROXY в .env или выбери другой proxy/VPN endpoint."
    )
    proxy_connection_failed: str = (
        "не удалось подключиться к TIKTOK_PROXY{proxy_hint}. "
        "Проверь, что proxy/VPN запущен и адрес в .env указан верно."
    )
    tiktok_invalid_json: str = (
        "TikTok API вернул пустой или невалидный ответ. "
        "Проверь TIKTOK_PROXY/VPN и повтори запрос."
    )
    download_timeout: str = "таймаут: {error}"
    download_internal_error: str = "внутренняя ошибка скачивания: {error}"

    # Limits
    photo_too_large: str = "фото слишком большое: {size_mb} MB, лимит {limit_mb} MB"
    video_too_large: str = "видео слишком большое: {size_mb} MB, лимит {limit_mb} MB"

    # Telegram service errors
    dump_chat_id_missing_file: str = (
        "внутренняя ошибка: DUMP_CHAT_ID не задан. "
        "Для inline-режима нужен служебный чат/канал, куда бот загрузит файл "
        "и получит Telegram file_id."
    )
    dump_chat_id_missing_video: str = (
        "внутренняя ошибка: DUMP_CHAT_ID не задан. "
        "Для inline-режима нужен служебный чат/канал, куда бот загрузит видео "
        "и получит Telegram file_id."
    )
    video_has_no_file: str = "внутренняя ошибка: у видео нет файла"
    album_has_no_files: str = "внутренняя ошибка: у фотоальбома нет файлов"
    telegram_no_video_file_id: str = "внутренняя ошибка: Telegram не вернул video.file_id"
    telegram_no_photo_file_id: str = "внутренняя ошибка: Telegram не вернул photo.file_id"
    unknown_media_kind: str = "внутренняя ошибка: неизвестный тип медиа {kind}"
    video_upload_failed: str = "ошибка отправки видео в Telegram после {retries} попыток: {error}"

    # Routing errors
    expected_video_got_album: str = "ссылка ведёт на фотоальбом, а ожидалось видео"


t = Lang()
```

---

## План внедрения

### Этап 1. Добавить `app/lang.py`

Просто создать файл и перенести туда все тексты. На этом этапе код ещё не трогать.

### Этап 2. Подключить `lang` в `handlers.py`

В начало файла:

```python
from app.lang import t
```

Заменить, например:

```python
await message.answer("Загрузка началась")
```

на:

```python
await message.answer(t.loading_started)
```

И:

```python
await message.answer(f"Ошибка: {error_text}")
```

на:

```python
await message.answer(t.error.format(error=error_text))
```

Стартовое сообщение:

```python
await message.answer(t.start.format(bot_username=settings.bot_username))
```

Inline title/description тоже через `t`.

### Этап 3. Подключить `lang` в `telegram_service.py`

В начало:

```python
from app.lang import t
```

Заменить:

```python
return f"таймаут {settings.download_timeout_seconds} секунд"
```

на:

```python
return t.timeout_seconds.format(seconds=settings.download_timeout_seconds)
```

И все `PublicBotError("...")` заменить на `PublicBotError(t.some_key)`.

### Этап 4. Подключить `lang` в `download_service.py`

В начало:

```python
from app.lang import t
```

В `classify_download_error()` заменить пользовательские ошибки на шаблоны:

```python
return t.ip_blocked
```

```python
return t.proxy_connection_failed.format(proxy_hint=proxy_hint)
```

```python
return t.download_internal_error.format(error=text[:500])
```

Логи внутри `download_service.py` не трогать.

### Этап 5. Убрать `PHOTO_CAPTION_TEMPLATE` или связать его с `lang`

Сейчас caption лежит в настройках как `PHOTO_CAPTION_TEMPLATE`, а не в коде. Это можно оставить как override через `.env`, но дефолт лучше брать из `lang`:

```python
photo_caption_template: str = os.getenv("PHOTO_CAPTION_TEMPLATE", t.photo_caption)
```

Но тут будет циклический импорт, если `config.py` импортирует `lang`, а `lang` где-то импортирует `settings`. Поэтому лучше так:

```python
# app/lang.py
photo_caption: str = "Original: {url}"

# app/telegram_service.py или handlers.py
caption = os.getenv("PHOTO_CAPTION_TEMPLATE", t.photo_caption).format(url=result.source_url)
```

Или оставить `settings.photo_caption_template` как есть, потому что это уже редактируется без кода через `.env`.

### Этап 6. Проверка

После замен:

```powershell
python -m py_compile .\app\lang.py
python -m py_compile .\app\handlers.py
python -m py_compile .\app\telegram_service.py
python -m py_compile .\app\download_service.py
python bot.py
```

Потом проверить сценарии:

```text
/start
обычная TikTok video ссылка
обычная TikTok photo ссылка
inline пустой запрос
inline video
inline slideshow photo/audio
ошибка с неправильной ссылкой
ошибка без DUMP_CHAT_ID
```

---

## Главное правило

В `lang.py` выносить всё, что увидит пользователь в Telegram.

Не выносить:

```python
logger.info(...)
logger.warning(...)
logger.exception(...)
```

И не трогать чисто технические строки вроде:

```python
"ACTIVE get_or_download_media FILE"
"TikTok media probe"
"Failed to process async inline video job"
```

Это консоль/диагностика, а не интерфейс бота.
