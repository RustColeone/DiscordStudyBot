"""WeChat account extension: the logged-in Linux WeChat client as an interface, plus a default persona.

Ported from the legacy bridge (bridges/wechat_bridge.py and services/wechat_*). Messages are read from the
account's encrypted local databases with a private key file; replies, files and login use the desktop
client through AT-SPI (`desktop_helper.py`). Which chats are answered, by whom and with which tools is
core configuration (admission rules and assignments), not interface logic.

`extension_config.wechat` (environment fallbacks in parentheses, from the legacy bridge):
  database_root      the account's `.../xwechat_files/wxid_…/db_storage`   (WECHAT_BRIDGE_DATABASE_ROOT)
  database_keys      private 0600 key file from key_setup.py               (WECHAT_BRIDGE_DATABASE_KEYS)
  python, display, wayland_display, runtime_dir    desktop helper environment (WECHAT_BRIDGE_PYTHON, …)
  max_upload_mb      local file budget, 1–100 (100)                         (WECHAT_BRIDGE_MAX_UPLOAD_MB)
  max_text_chars     message length the engine aims for (500)
  poll_seconds (1), status_seconds (15), coalesce_seconds (1, 0–5)        (WECHAT_BRIDGE_COALESCE_SECONDS)
  account_interval_s (20), chat_interval_s (20)   minimum spacing of the bot's own messages
  submit_media (false)     submit incoming images/files/stickers as requests instead of only recording them
  observe_chatter (false)  record group messages not addressed to the account in history
  legacy_database          bot_data.db: import the legacy read positions once (switchover)
"""

import os
from pathlib import Path

from contracts import Contributions, Extension

from .interface import WeChatInterface
from .persona import PERSONA

ENVIRONMENT = {"database_root": "WECHAT_BRIDGE_DATABASE_ROOT", "database_keys": "WECHAT_BRIDGE_DATABASE_KEYS",
               "python": "WECHAT_BRIDGE_PYTHON", "display": "WECHAT_BRIDGE_DISPLAY",
               "wayland_display": "WECHAT_BRIDGE_WAYLAND_DISPLAY", "runtime_dir": "WECHAT_BRIDGE_RUNTIME_DIR",
               "max_upload_mb": "WECHAT_BRIDGE_MAX_UPLOAD_MB", "coalesce_seconds": "WECHAT_BRIDGE_COALESCE_SECONDS"}
DEFAULTS = {"python": "/usr/bin/python3", "display": ":0", "wayland_display": "wayland-0", "runtime_dir": "",
            "max_upload_mb": 100.0, "max_text_chars": 500, "poll_seconds": 1.0, "status_seconds": 15.0,
            "coalesce_seconds": 1.0, "account_interval_s": 20.0, "chat_interval_s": 20.0, "submit_media": False,
            "observe_chatter": False, "legacy_database": ""}
NUMBERS = {"max_upload_mb": (1, 100), "max_text_chars": (20, 4000), "poll_seconds": (0.2, 60),
           "status_seconds": (1, 300), "coalesce_seconds": (0, 5), "account_interval_s": (0, 3600),
           "chat_interval_s": (0, 3600)}


def settings_from(config, data_dir: Path, environ=os.environ) -> dict:
    settings = dict(DEFAULTS)
    for key, value in config.items():
        settings[key] = value
    for key, name in ENVIRONMENT.items():
        if key not in config and environ.get(name, "").strip():
            settings[key] = environ[name].strip()
    for key, (low, high) in NUMBERS.items():
        settings[key] = max(low, min(high, float(settings[key])))
    settings["max_text_chars"] = int(settings["max_text_chars"])
    data_dir = Path(data_dir)
    settings.setdefault("database_root", "")
    settings["database_keys"] = str(settings.get("database_keys") or data_dir / "keys.json")
    settings["outbox_dir"] = str(settings.get("outbox_dir") or data_dir / "outbox")
    settings["login_dir"] = str(settings.get("login_dir") or data_dir / "login")
    return settings


def setup(context) -> Contributions:
    return Contributions(interfaces=[WeChatInterface(settings_from(context.config, context.data_dir))],
                         personas=[PERSONA])


EXTENSION = Extension("wechat", setup, contracts="1", version="1",
                      description="WeChat account: native database reads, desktop sends, owner operations")
