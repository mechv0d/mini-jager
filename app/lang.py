from dataclasses import dataclass


@dataclass(frozen=True)
class Lang:
    # Common
    error: str = "Ошибка: {error}"
    internal_error: str = "внутренняя ошибка: {type}: {message}"
    timeout_seconds: str = "таймаут {seconds} секунд"

    # Start / messages
    start: str = (
        "Привет! Отправь ссылку на TikTok-видео или слайдшоу, "
        "и я отправлю медиа сюда.\n\n"
        "Также можно использовать inline-режим: @{bot_username} https://www.tiktok.com/..."
    )
    loading_started: str = "Загрузка началась"

    # Inline common
    inline_loading_button: str = "Загрузка выполняется"
    inline_loading_callback: str = "Медиа ещё загружается. Сообщение обновится автоматически."
    inline_help_title: str = "Вставь ссылку TikTok"
    inline_help_description: str = "Пример: @{bot_username} https://www.tiktok.com/..."
    inline_help_message: str = "Пришли ссылку на TikTok-видео или слайдшоу."

    # Inline video
    inline_send_video_title: str = "Отправить видео"
    inline_video_ready: str = "Видео готово"
    inline_download_title: str = "Скачать TikTok"
    inline_download_description: str = "Видео появится после загрузки"
    inline_download_message: str = "Загрузка началась. Медиа появится здесь автоматически."

    # Inline photo/audio
    inline_photo_title: str = "Фото {index}"
    inline_video_slide_title: str = "Видео {index}"
    inline_audio_title: str = "Аудио"
    inline_photo_description: str = "Фото из TikTok-слайдшоу"
    inline_album_ready_retry: str = "Слайдшоу готово."
    inline_photo_prepare_failed_title: str = "Не удалось подготовить фото"
    inline_photo_prepare_failed_description: str = "{error}"

    # Captions
    photo_caption: str = "Ссылка: {url}"

    # Download errors
    invalid_tiktok_url: str = "невалидная ссылка: поддерживаются только ссылки TikTok"
    ip_blocked: str = (
        "T1kTok за9локировал тек8щий IP-4дрес. "
        "Ошибка прокси/VPN."
    )
    proxy_connection_failed: str = (
        "не удалось подключиться к TIKTOK_PROXY{proxy_hint}. "
    )
    tiktok_invalid_json: str = (
        "TikTok API вернул пустой или невалидный ответ. "
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
    album_has_no_files: str = "внутренняя ошибка: у слайдшоу нет файлов"
    telegram_no_video_file_id: str = "внутренняя ошибка: Telegram не вернул video.file_id"
    telegram_no_photo_file_id: str = "внутренняя ошибка: Telegram не вернул photo.file_id"
    unknown_media_kind: str = "внутренняя ошибка: неизвестный тип медиа {kind}"
    video_upload_failed: str = "ошибка отправки видео в Telegram после {retries} попыток: {error}"

    # Routing errors
    expected_video_got_album: str = "ссылка ведёт на слайдшоу, а ожидалось видео"


t = Lang()
