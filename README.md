# TikTok Video Downloader Telegram Bot

A Telegram bot for downloading TikTok videos from user-provided links. The bot supports direct messages, group chats, inline mode, local file caching, download timeouts, cache expiration, and console error logging.

The project is built with Python, `aiogram 3`, and `yt-dlp`.

## Features

* `/start` command with a welcome message
* TikTok URL validation
* Direct video download from TikTok links
* User-facing loading status message
* Automatic removal of the loading message after completion or failure
* Sends downloaded videos without captions
* 15-second download timeout by default
* Error reporting with HTTP status, timeout, or internal error details
* Console logging for failed downloads
* Optional local cache support
* Cache lookup by TikTok video ID or URL hash
* Automatic cache cleanup using a background heartbeat task
* Inline mode support for usage in any chat via `@bot_username <TikTok URL>`

## Important Telegram Inline Mode Note

Telegram inline mode works differently from regular chat messages.

When a user writes:

```text
@ttsavefrom_bot https://www.tiktok.com/t/gKFv35lMsx/
```

the bot receives an inline query and returns one or more inline results. The user then selects the result, and Telegram inserts it into the current chat.

Because Telegram does not provide the target chat ID to the bot during inline queries, the bot cannot send or delete a `Download started` message in that chat. Instead, the bot downloads the video, prepares a cached Telegram video result, and returns it as an inline result.

For inline mode to work reliably, the bot uses a service chat or private channel specified by `DUMP_CHAT_ID`. The bot uploads the video there first, receives a reusable Telegram `file_id`, and then returns that file as an inline cached video result.

## Requirements

* Python 3.11 or newer
* Telegram bot token from BotFather
* Inline mode enabled in BotFather
* `aiogram`
* `yt-dlp`

Install dependencies:

```bash
pip install -U -r requirements.txt
```

## Installation

Clone the repository:

```bash
git clone https://github.com/mechv0d/mini-jager.git
cd mini-jager
```

Create a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install requirements:

```bash
pip install -U -r requirements.txt
```

Create `requirements.txt`:

```txt
aiogram>=3.22.0
yt-dlp>=2026.06.09
```

## Configuration

The bot is configured through environment variables.

| Variable                       |                 Required | Default          | Description                                                                                    |
| ------------------------------ | -----------------------: | ---------------- | ---------------------------------------------------------------------------------------------- |
| `BOT_TOKEN`                    |                      Yes | None             | Telegram bot token from BotFather                                                              |
| `BOT_USERNAME`                 |              Recommended | `ttsavefrom_bot` | Bot username without `@`                                                                       |
| `ENABLE_CASH`                  |                       No | `1`              | Enables local video cache when set to `1`                                                      |
| `CACHE_DIR`                    |                       No | `./cache`        | Directory for cached video files and cache index                                               |
| `CACHE_TTL_SECONDS`            |                       No | `86400`          | Time in seconds before cached videos are deleted                                               |
| `CACHE_CLEAN_INTERVAL_SECONDS` |                       No | `60`             | Interval in seconds for cache cleanup heartbeat                                                |
| `DOWNLOAD_TIMEOUT_SECONDS`     |                       No | `15`             | Download timeout in seconds                                                                    |
| `DUMP_CHAT_ID`                 | Required for inline mode | None             | Service chat/channel ID for receiving uploaded videos and generating Telegram `file_id` values |
| `MAX_VIDEO_SIZE_MB`            |                       No | `48`             | Maximum video size for Telegram upload                                                         |

Example:

```bash
export BOT_TOKEN="123456789:ABCDEF..."
export BOT_USERNAME="ttsavefrom_bot"
export ENABLE_CASH="1"
export CACHE_TTL_SECONDS="86400"
export CACHE_CLEAN_INTERVAL_SECONDS="60"
export DOWNLOAD_TIMEOUT_SECONDS="15"
export DUMP_CHAT_ID="-1001234567890"
export MAX_VIDEO_SIZE_MB="48"
```

## BotFather Setup

Create a bot with BotFather and configure the following settings:

1. Create a new bot using `/newbot`.
2. Enable inline mode using `/setinline`.
3. Optional for group usage: disable privacy mode using `/setprivacy` if the bot should process regular TikTok links in groups.
4. Add the bot to the service chat or private channel used as `DUMP_CHAT_ID`.
5. Ensure the bot has permission to send videos in the service chat or channel.

## Usage

### Direct Message Usage

Send a TikTok link directly to the bot:

```text
https://www.tiktok.com/t/gKFv35lMsx/
```

The bot will:

1. Validate the URL.
2. Send `Download started`.
3. Download the video.
4. Delete the loading message.
5. Send the downloaded video without a caption.

If the download fails or exceeds the timeout, the bot deletes the loading message and sends an error message.

### Group Chat Usage

If the bot is added to a group and has permission to read messages, users can send a TikTok link directly in the group.

Example:

```text
https://www.tiktok.com/t/gKFv35lMsx/
```

The bot will process the link and send the resulting video to the same group.

### Inline Mode Usage

The bot can be used from any Telegram chat that supports inline bots:

```text
@ttsavefrom_bot https://www.tiktok.com/t/gKFv35lMsx/
```

Telegram will display an inline result. After the user selects the result, the video is sent to the current chat.

## Cache Behavior

Caching is controlled by the `ENABLE_CASH` option.

When `ENABLE_CASH=1`, the bot follows this flow:

1. The user sends a TikTok link.
2. The bot extracts or resolves a unique TikTok video ID.
3. The bot checks the local cache for an existing downloaded video.
4. If the video exists in cache, it is sent immediately.
5. If the video does not exist in cache, the bot downloads it.
6. The downloaded video is saved to the cache directory.
7. The cache index is updated with the TikTok video ID, source URL, local file path, creation time, and Telegram `file_id` if available.

The cache heartbeat periodically removes expired files based on `CACHE_TTL_SECONDS`.

## Error Handling

The bot reports user-visible errors in the following format:

```text
Error: <error details>
```

Possible error categories include:

* HTTP errors, for example `HTTP 403` or `HTTP 404`
* Download timeout
* Invalid TikTok URL
* Internal download error
* Telegram upload error
* Missing `DUMP_CHAT_ID` for inline mode

All download and processing errors are also logged to the console.

## Running the Bot

Start the bot with:

```bash
python bot.py
```

For production usage, run it under a process manager such as `systemd`, Docker, Supervisor, or PM2.

## Production Notes

For a production deployment, consider the following:

* Keep `yt-dlp` updated regularly.
* Use a persistent volume for `CACHE_DIR`.
* Monitor disk usage if cache is enabled.
* Configure log collection.
* Use a private service channel for `DUMP_CHAT_ID`.
* Set a reasonable `CACHE_TTL_SECONDS` value.
* Avoid setting the video size limit too close to Telegram upload limits.
* Respect platform terms of service and copyright requirements.

## Legal Notice

This bot is intended for personal and lawful use only. Users are responsible for ensuring they have the right to download, store, and share any content processed through the bot. The maintainers of this project are not responsible for misuse of the software.

## License

Specify your project license here.

Example:

```text
MIT License
```
