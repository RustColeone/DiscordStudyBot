import argparse

from app.bot_app import BotApplication
from services.config_service import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bot with a selected platform adapter.")
    parser.add_argument("--mode", choices=["discord", "wechat"], default="discord", help="Adapter mode to run")
    args = parser.parse_args()

    config = load_config()
    application = BotApplication(config)

    if args.mode == "discord":
        from adapters.discord_adapter import DiscordAdapter

        adapter = DiscordAdapter(application, config)
    elif args.mode == "wechat":
        from adapters.wechat_adapter import WeChatAdapter

        adapter = WeChatAdapter(application, config)
    else:
        raise ValueError(f"Unsupported mode: {args.mode}")

    adapter.run()


if __name__ == "__main__":
    main()
