import os

from config import _read_env


def test_read_env_prefers_process_environment(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "railway:token")
    assert _read_env("BOT_TOKEN", "") == "railway:token"


def test_read_env_strips_wrapping_quotes(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("BOT_TOKEN", '"railway:token"')
    assert _read_env("BOT_TOKEN", "") == "railway:token"
