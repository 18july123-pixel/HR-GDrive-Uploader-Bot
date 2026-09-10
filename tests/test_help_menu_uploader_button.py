from bot.keyboards import help_menu


def test_help_menu_contains_uploader_callback_button():
    markup = help_menu()
    assert markup is not None
    # Button payloads in aiogram inline markups are stored as callback_data
    # in the underlying inline keyboard rows; verify one concrete action pair.
    buttons = []
    for row in markup.inline_keyboard:
        buttons.extend(row)
    callback_values = {btn.callback_data for btn in buttons if hasattr(btn, 'callback_data')}
    assert 'menu:uploader' in callback_values
