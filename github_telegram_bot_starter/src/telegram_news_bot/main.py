from __future__ import annotations

import argparse
from pprint import pprint

from .automation import AutomationDaemon
from .config import load_settings
from .instance_lock import InstanceLock, InstanceLockError
from .logging_utils import configure_logging
from .pipeline import NewsTelegramService
from .telegram_client import TelegramPublisher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scheduled news collector and Telegram publisher."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("show-config", help="Show the active runtime configuration.")
    subparsers.add_parser(
        "discover-channel",
        help="Read Telegram updates and print detected channel IDs.",
    )
    subparsers.add_parser(
        "discover-admin",
        help="Read Telegram updates and print private chat IDs for admin binding.",
    )

    run_once = subparsers.add_parser(
        "run-once",
        help="Fetch candidates and publish the next batch once.",
    )
    run_once.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and print the selected posts without sending them to Telegram.",
    )

    subparsers.add_parser(
        "serve",
        help="Run the scheduler loop and publish automatically on the configured interval.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings()
    log_file = configure_logging(
        settings.logs_directory,
        secrets=[settings.telegram_bot_token],
    )

    if args.command == "show-config":
        runtime_config = NewsTelegramService(settings).load_runtime_config()
        pprint(
            {
                "telegram_channel_id": settings.telegram_channel_id,
                "telegram_admin_chat_id": settings.telegram_admin_chat_id,
                "telegram_admin_pair_code_configured": settings.telegram_admin_pair_code is not None,
                "llm_provider": settings.llm_provider,
                "opencode_model": settings.opencode_model,
                "ollama_base_url": settings.ollama_base_url,
                "ollama_model": settings.ollama_model,
                "quiet_hours_start_hour": settings.quiet_hours_start_hour,
                "quiet_hours_end_hour": settings.quiet_hours_end_hour,
                "runtime_llm_provider": runtime_config.llm_provider,
                "runtime_opencode_model": runtime_config.opencode_model,
                "runtime_ollama_model": runtime_config.ollama_model,
                "runtime_admin_chat_id": runtime_config.admin_chat_id,
                "runtime_post_interval_minutes": runtime_config.post_interval_minutes,
                "runtime_search_window_hours": runtime_config.search_window_hours,
                "runtime_quiet_hours_start_hour": runtime_config.quiet_hours_start_hour,
                "runtime_quiet_hours_end_hour": runtime_config.quiet_hours_end_hour,
                "database_path": str(settings.database_path),
                "logs_directory": str(settings.logs_directory),
                "post_interval_minutes": settings.post_interval_minutes,
                "search_window_hours": settings.search_window_hours,
                "max_posts_per_run": settings.max_posts_per_run,
                "source_feed_urls": settings.source_feed_urls,
                "source_api_token_configured": settings.source_api_token is not None,
            }
        )
        return

    if args.command == "discover-channel":
        publisher = TelegramPublisher(settings.telegram_bot_token)
        channels = publisher.discover_channels()

        if not channels:
            print(
                "No channel updates found. Publish any manual test post in the channel and run this command again."
            )
            return

        for channel in channels:
            pprint(channel)
        return

    if args.command == "discover-admin":
        publisher = TelegramPublisher(settings.telegram_bot_token)
        private_chats = publisher.discover_private_chats()

        if not private_chats:
            print(
                "No private chat updates found. Write /start to the bot in a private chat and run this command again."
            )
            return

        for private_chat in private_chats:
            pprint(private_chat)
        return

    service = NewsTelegramService(settings)

    if args.command == "run-once":
        runtime_config = service.load_runtime_config()
        if not args.dry_run:
            pause_until = service.get_quiet_hours_pause_until(runtime_config)
            if pause_until is not None:
                print(
                    "Automatic publication is paused by quiet hours until "
                    f"{pause_until.astimezone().strftime('%Y-%m-%d %H:%M local time')}."
                )
                return

        results = service.run_once(dry_run=args.dry_run, runtime_config=runtime_config)
        if not results:
            print("No new candidates were selected.")
            return

        for result in results:
            print(f"[{result.item.score:0.2f}] {result.item.title}")
            if result.published:
                print(f"Published message_id={result.message_id}")
            print(result.caption)
            print()
        return

    if not settings.telegram_channel_id:
        raise SystemExit("TELEGRAM_CHANNEL_ID is required for the scheduler mode.")

    lock_path = settings.logs_directory / "serve.lock"
    try:
        with InstanceLock(lock_path):
            daemon = AutomationDaemon(settings, service, log_file)
            daemon.run()
    except InstanceLockError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()