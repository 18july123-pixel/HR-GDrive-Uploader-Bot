from config import cfg
import html
import logging

from aiogram.exceptions import TelegramBadRequest

log = logging.getLogger("gdrive_bot.telegram")


def is_admin(user_id: int) -> bool:
    return user_id in cfg.ADMIN_IDS


def user_message(call):
    """Many callback handlers reuse a /command handler by calling it with
    `call.message`. But `call.message` is the message the BOT sent (the one
    with the inline keyboard on it) - its `.from_user` is the bot itself, not
    the person who tapped the button. Any handler that reads
    `message.from_user.id` off that object silently operates as the bot
    (writing bot rows into the users table, sending Google's OAuth `state` as
    the bot's own Telegram ID, etc.), which is what eventually causes
    Telegram to reject a later send with "Forbidden: bot can't send messages
    to bot".

    This returns a copy of `call.message` with `.from_user` corrected to
    `call.from_user` (the real, clicking user), so it's safe to pass into
    those handlers.
    """
    return call.message.model_copy(deep=True, update={"from_user": call.from_user})


def human_bytes(n: int | float | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, remaining = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {remaining:02d}s"
    return f"{remaining}s"


def html_link(label: str, url: str | None) -> str:
    """Return a Telegram HTML link with both label and URL safely escaped."""
    safe_label = html.escape(str(label), quote=False)
    if not url:
        return safe_label
    return f'<a href="{html.escape(url, quote=True)}">{safe_label}</a>'


async def safe_edit_text(message, text: str, **kwargs) -> bool:
    """Edit a message while ignoring harmless Telegram no-op edit errors."""
    try:
        await message.edit_text(text, **kwargs)
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return False
        raise


async def safe_answer(callback, text: str | None = None, **kwargs) -> bool:
    """Acknowledge a callback safely, including callbacks already answered."""
    try:
        await callback.answer(text, **kwargs)
        return True
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        if (
            "query is too old" in error_text
            or "query id is invalid" in error_text
            or "already answered" in error_text
        ):
            return False
        raise


def progress_bar(
    done: int,
    total: int,
    width: int = 12,
    done_bytes: int | float | None = None,
    total_bytes: int | float | None = None,
) -> str:
    """Render a percentage bar and optional transferred/total byte sizes."""
    if total <= 0:
        result = "░" * width + " 0%"
    else:
        pct = min(done / total, 1.0)
        filled = int(pct * width)
        result = "█" * filled + "░" * (width - filled) + f" {int(pct * 100)}%"
    if total_bytes is not None:
        result += f" ({human_bytes(done_bytes or 0)} / {human_bytes(total_bytes)})"
    return result
