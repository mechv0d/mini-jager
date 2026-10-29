import time
from dataclasses import dataclass

from aiogram.types import Chat, Message, User

from app.config import settings
from app.lang import t

_INLINE_CONTEXT_TTL_SECONDS = 30 * 60

# Inline queries do not contain the target chat. Telegram only reports the chat
# type ("sender", "private", "group", ...). For groups/channels we additionally
# remember the chat from the service message with via_bot that Telegram sends
# when the inline result is inserted. This is best-effort: bots do not receive
# the inserted message in a private chat between two other users.
_inline_chat_types: dict[tuple[int, str], tuple[float, str | None]] = {}
_inline_recipients: dict[int, tuple[float, str, str]] = {}


@dataclass(frozen=True)
class DumpMetadata:
    source_url: str
    sender: str
    recipient: str

    def caption(self) -> str:
        return t.dump_caption.format(
            source_url=self.source_url,
            sender=self.sender,
            recipient=self.recipient,
        )


def display_name(user: User | None) -> str:
    if not user:
        return t.unknown_user

    if user.username:
        return f"@{user.username}"

    return user.full_name.strip() or str(user.id)


def format_user(user: User | None) -> str:
    if not user:
        return t.unknown_user

    name = user.full_name.strip()
    username = f"@{user.username}" if user.username else ""

    if username and name:
        return f"{username} ({name})"

    return username or name or str(user.id)


def format_chat(chat: Chat | None) -> str:
    if not chat:
        return t.unknown_chat

    title = (chat.title or chat.full_name or "").strip()
    if title:
        return f"{chat.id} - {title}"

    return str(chat.id)


def recipient_for_message(message: Message) -> str:
    if message.chat.type in {"group", "supergroup", "channel"}:
        return format_chat(message.chat)

    # A normal message request is processed in the bot's private chat.
    return f"@{settings.bot_username}"


def metadata_for_message(message: Message, source_url: str) -> DumpMetadata:
    if message.from_user:
        sender = format_user(message.from_user)
    elif message.sender_chat:
        sender = format_chat(message.sender_chat)
    else:
        sender = t.unknown_user

    return DumpMetadata(
        source_url=source_url,
        sender=sender,
        recipient=recipient_for_message(message),
    )


def _prune_inline_state(now: float) -> None:
    for key, (created_at, _value) in list(_inline_chat_types.items()):
        if now - created_at > _INLINE_CONTEXT_TTL_SECONDS:
            _inline_chat_types.pop(key, None)

    for key, item in list(_inline_recipients.items()):
        if now - item[0] > _INLINE_CONTEXT_TTL_SECONDS:
            _inline_recipients.pop(key, None)

def remember_inline_query(user_id: int, query: str, chat_type: str | None) -> None:
    now = time.time()
    _prune_inline_state(now)
    _inline_chat_types[(user_id, query)] = (now, chat_type)
    _inline_recipients.pop(user_id, None)


def get_inline_chat_type(user_id: int, query: str) -> str | None:
    now = time.time()
    _prune_inline_state(now)

    item = _inline_chat_types.get((user_id, query))
    return item[1] if item else None


def has_inline_recipient(user_id: int, chat_type: str | None) -> bool:
    now = time.time()
    _prune_inline_state(now)
    stored = _inline_recipients.get(user_id)

    if not stored:
        return False

    if chat_type in {"group", "supergroup", "channel"}:
        return stored[2] in {"group", "supergroup", "channel"}

    if chat_type == "sender":
        return stored[2] == "private"

    return True


def remember_via_bot_message(message: Message) -> None:
    if not message.via_bot or not message.from_user:
        return

    if (message.via_bot.username or "").lower() != settings.bot_username.lower():
        return

    now = time.time()
    _prune_inline_state(now)
    _inline_recipients[message.from_user.id] = (
        now,
        recipient_for_message(message),
        message.chat.type,
    )


def resolve_inline_recipient(user: User, chat_type: str | None) -> str:
    now = time.time()
    _prune_inline_state(now)
    stored = _inline_recipients.get(user.id)

    if chat_type in {"group", "supergroup", "channel"}:
        if stored and stored[2] in {"group", "supergroup", "channel"}:
            return stored[1]
        return t.inline_group_recipient

    if chat_type == "sender":
        # "sender" means a private chat with the inline query sender. It can be
        # the bot chat (the via_bot message is visible to us) or Saved Messages.
        if stored and stored[2] == "private":
            return stored[1]
        return format_user(user)

    if chat_type == "private":
        # Private chat with another user. Telegram does not disclose its id.
        return t.inline_private_recipient

    if stored:
        return stored[1]

    return t.inline_unknown_recipient


def metadata_for_inline(user: User, source_url: str, chat_type: str | None) -> DumpMetadata:
    return DumpMetadata(
        source_url=source_url,
        sender=format_user(user),
        recipient=resolve_inline_recipient(user, chat_type),
    )
