import asyncio
import threading

from utils import schedule_safe_edit_text


class DummyMessage:
    async def edit_text(self, text, **kwargs):
        return True


def test_schedule_safe_edit_text_uses_captured_loop():
    loop = asyncio.new_event_loop()
    msg = DummyMessage()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    try:
        future = schedule_safe_edit_text(loop, msg, "Uploading test", parse_mode="HTML")
        assert future.result(timeout=2) is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()
