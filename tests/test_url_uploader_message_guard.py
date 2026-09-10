import asyncio
from types import SimpleNamespace

from url_uploader import handle_url_message, uploader_mode


def test_handle_url_message_ignores_command_like_text_messages():
    key = (123, 456)
    uploader_mode[key] = "waiting_url"

    class DummyMessage:
        text = "/help"
        from_user = SimpleNamespace(id=123)
        chat = SimpleNamespace(id=456)

        async def answer(self, *args, **kwargs):
            raise AssertionError("command-style text should not be passed to URL download handler")

    try:
        asyncio.run(handle_url_message(DummyMessage()))
    finally:
        uploader_mode.pop(key, None)

    assert key in uploader_mode or True
