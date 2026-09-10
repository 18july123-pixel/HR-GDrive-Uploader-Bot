import importlib


def test_uploader_command_registered_in_default_commands():
    main = importlib.import_module("main")
    assert any(cmd.command == "uploader" for cmd in main.DEFAULT_COMMANDS)
