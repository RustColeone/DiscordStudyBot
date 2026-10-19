"""WeChat account extension: the logged-in Linux WeChat client as an interface, plus a default persona.

Messages are read from the account's encrypted local
databases with a private key file; replies, files and login use the desktop client through AT-SPI
(`desktop_helper.py`). Which chats are answered, by whom and with which tools is
core configuration (admission rules and assignments), not interface logic.

`extension_config.wechat` (environment fallbacks in parentheses):
  database_root      the account's `.../xwechat_files/wxid_…/db_storage`   (WECHAT_BRIDGE_DATABASE_ROOT)
  database_keys      private 0600 key file from key_setup.py               (WECHAT_BRIDGE_DATABASE_KEYS)
  python, display, wayland_display, runtime_dir    desktop helper environment (WECHAT_BRIDGE_PYTHON, …)
  max_upload_mb      local file budget, 1–100 (100)                         (WECHAT_BRIDGE_MAX_UPLOAD_MB)
  max_text_chars     message length the engine aims for (500)
  poll_seconds (1), status_seconds (15), coalesce_seconds (1, 0–5)        (WECHAT_BRIDGE_COALESCE_SECONDS)
  account_interval_s (20), chat_interval_s (20)   minimum spacing of the bot's own messages
  submit_media (false)     submit incoming images/files/stickers as requests instead of only recording them
  observe_chatter (false)  record group messages not addressed to the account in history
  image_key                private 0600 file from image_key.py; without it received images stay "[Image]"
  images_dir               where decrypted copies of received images are kept
"""

import os
from pathlib import Path

from contracts import Contributions, Extension, Setting

from .interface import WeChatInterface
from .persona import PERSONA

ENVIRONMENT = {"database_root": "WECHAT_BRIDGE_DATABASE_ROOT", "database_keys": "WECHAT_BRIDGE_DATABASE_KEYS",
               "python": "WECHAT_BRIDGE_PYTHON", "display": "WECHAT_BRIDGE_DISPLAY",
               "wayland_display": "WECHAT_BRIDGE_WAYLAND_DISPLAY", "runtime_dir": "WECHAT_BRIDGE_RUNTIME_DIR",
               "max_upload_mb": "WECHAT_BRIDGE_MAX_UPLOAD_MB", "coalesce_seconds": "WECHAT_BRIDGE_COALESCE_SECONDS"}
DEFAULTS = {"python": "/usr/bin/python3", "display": ":0", "wayland_display": "wayland-0", "runtime_dir": "",
            "max_upload_mb": 100.0, "max_text_chars": 500, "poll_seconds": 1.0, "status_seconds": 15.0,
            "coalesce_seconds": 1.0, "account_interval_s": 20.0, "chat_interval_s": 20.0, "submit_media": False,
            "observe_chatter": False}
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
    settings["image_key"] = str(settings.get("image_key") or data_dir / "image_key.json")
    settings["images_dir"] = str(settings.get("images_dir") or data_dir / "images")
    return settings


def setup(context) -> Contributions:
    if not context.config and not any(os.environ.get(name, "").strip() for name in ENVIRONMENT.values()):
        # A drop-in every install has: stay out of the way until WeChat is configured.
        context.log("not_configured")
        return Contributions()
    # Defaults core.yml overrides: no chat is answered until core.yml opens it (`admission`, chat by chat), and
    # the chats it opens get the assistant as 本鲸 (this module's persona).
    return Contributions(interfaces=[WeChatInterface(settings_from(context.config, context.data_dir))],
                         personas=[PERSONA], admission={"wechat:*": {"enabled": False}},
                         assignments={"wechat:*": {"engine": "assistant", "personas": [PERSONA.name]}})


EXTENSION = Extension("wechat", setup, contracts="1", version="1",
                      description="WeChat account: native database reads, desktop sends, owner operations", settings=(
    Setting("database_root", "The account's …/xwechat_files/wxid_…/db_storage folder (see docs/WECHAT.md)", "path"),
    Setting("database_keys", "Key file from key_setup (default: in the module's data folder)", "path"),
    Setting("max_text_chars", "Length replies aim for", "number", 500),
))
