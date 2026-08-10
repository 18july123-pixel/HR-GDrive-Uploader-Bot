import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

import database as db
from utils import is_admin, safe_answer

log = logging.getLogger("gdrive_bot.middleware")
_RATE_WINDOW_SECONDS = 60
_RATE_LIMIT = 30
_event_times: dict[int, deque[float]] = defaultdict(deque)
_rate_lock = asyncio.Lock()


async def _reply(event: TelegramObject, text: str) -> None:
    """Send a text reply appropriate for the event type.

    FIX #8: hasattr(event, "answer") is unreliable — both Message and
    CallbackQuery have an .answer() method but with completely different
    semantics (new chat message vs. popup notification).  Use explicit
    isinstance checks so the user always sees a proper chat message.
    """
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        # send a proper chat message via the underlying message, then
        # acknowledge the callback so the spinner on the button clears.
        if event.message:
            await event.message.answer(text)
        await safe_answer(event)


class GateMiddleware(BaseMiddleware):
    """Blocks banned users, and respects global bot on/off + maintenance mode.
    Admins always pass through so they can run /bot_on, /bot_off, etc.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,  # Message or CallbackQuery (not raw Update)
        data: Dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        if is_admin(user.id):
            return await handler(event, data)

        db_user = await asyncio.to_thread(db.get_user, user.id)
        if db_user and db_user.get("is_banned"):
            return  # silently drop

        bot_enabled = await asyncio.to_thread(db.get_state, "bot_enabled")
        if bot_enabled == "0":
            await _reply(event, "🔴 The bot is currently disabled by the admin. Please try again later.")
            return

        maintenance = await asyncio.to_thread(db.get_state, "maintenance")
        if maintenance == "1":
            await _reply(event, "🛠️ The bot is under maintenance. Please try again shortly.")
            return

        return await handler(event, data)


class CallbackAckMiddleware(BaseMiddleware):
    """Clear Telegram's button spinner before slow handler work starts."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            await safe_answer(event)
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Limit bursts without slowing normal command usage."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or is_admin(user.id):
            return await handler(event, data)

        now = time.monotonic()
        async with _rate_lock:
            events = _event_times[user.id]
            while events and now - events[0] >= _RATE_WINDOW_SECONDS:
                events.popleft()
            if len(events) >= _RATE_LIMIT:
                await _reply(event, "⏳ Too many requests. Please wait a moment and try again.")
                return
            events.append(now)
        return await handler(event, data)


class ExceptionMiddleware(BaseMiddleware):
    """Keep one bad update from producing an unhandled webhook failure."""

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except Exception:
            log.exception("Unhandled bot error while processing %s", type(event).__name__)
            if isinstance(event, Message):
                await event.answer("⚠️ Something went wrong. Please try again.")
            elif isinstance(event, CallbackQuery):
                if event.message:
                    await event.message.answer("⚠️ Something went wrong. Please try again.")
                await safe_answer(event)
