import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_telegram_stub():
    """Install the tiny Telegram SDK surface needed by offline tests.

    Several TG UI tests used to duplicate this stub locally. Keeping it here
    makes the test suite easier to maintain and avoids copy/paste drift.
    """
    import types

    if "telegram" not in sys.modules:
        telegram_stub = types.ModuleType("telegram")

        class InlineKeyboardButton:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data

        class InlineKeyboardMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

        telegram_stub.Update = object
        telegram_stub.InlineKeyboardButton = InlineKeyboardButton
        telegram_stub.InlineKeyboardMarkup = InlineKeyboardMarkup
        sys.modules["telegram"] = telegram_stub

    if "telegram.ext" not in sys.modules:
        ext_stub = types.ModuleType("telegram.ext")

        class _Builder:
            def token(self, token):
                return self

            def build(self):
                class App:
                    def add_handler(self, *args, **kwargs):
                        return None

                return App()

        class Application:
            @classmethod
            def builder(cls):
                return _Builder()

        class CommandHandler:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        ext_stub.Application = Application
        ext_stub.CommandHandler = CommandHandler
        ext_stub.CallbackQueryHandler = CommandHandler
        ext_stub.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
        sys.modules["telegram.ext"] = ext_stub


_install_telegram_stub()
