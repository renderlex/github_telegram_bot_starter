# Telegram News Bot Starter

This is a zero-dependency starter project designed for publishing in your own repository. This folder is clean of any personal Telegram credentials, local SQLite databases, logs, virtual environments (.venv), editor artifacts (.lh), or private files. It is fully prepared so that another user can clone it as a baseline to build their own bot, channel, news sources, and configuration.

## Key Features

* **Automated Workflow**: Automatically fetches news from connected sources and formats posts for your Telegram channel.
* **Local Storage**: Stores runtime configuration and application state in a local SQLite database.
* **Telegram Control Panel**: Supports full management and adjustments directly via a private chat with the bot.
* **LLM Integration**: Built-in support for OpenCode, Ollama, or a template-based fallback system.
* **Runtime Customization**: Change publishing intervals, quiet hours, LLM providers, and models on the fly directly from Telegram.

## About News Sources

By default, this starter includes a neutral feed collector that reads sources defined in your configuration. This is not a hard limitation; you can keep using RSS/Atom streams or entirely replace the collector with your own custom API client or web scraper.

You can adapt this starter for:
* RSS/Atom feeds of any website.
* Proprietary or commercial news APIs.
* Content streams and newsletters from other platforms.
* Curated lists of thematic blogs and sites.

The core framework (Telegram management, SQLite state, logging, quiet hours, and LLM menus) remains completely independent of the specific news source you choose.

## Repository Cleanliness

The following private and local artifacts have been strictly excluded from this starter template:
* No `.env` files containing credentials.
* No operational `data/bot.sqlite3` database.
* No local `logs/` directory.
* No `.lh/` or other local IDE/text editor history files.
* No local testing or validation scripts.

## Quick Start

1. Clone this starter template into your own private or public repository.
2. Create a new Telegram bot via BotFather and obtain a unique `TELEGRAM_BOT_TOKEN`.
3. Set up a Telegram channel where the bot will have administrative privileges to publish posts.
4. Add your preferred RSS/Atom URLs to `SOURCE_FEED_URLS` or set up a custom collector.
5. Copy the `.env.example` file into a new file named `.env`.
6. Fill in your personal configuration values.
7. Run the automated script `run_telegram_news_bot.bat` or manually install the dependencies and execute the server.

## Configuration Environment Variables

Create your local `.env` file using the following structure:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHANNEL_ID=
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_ADMIN_PAIR_CODE=
SOURCE_API_TOKEN=
SOURCE_FEED_URLS=
LLM_PROVIDER=auto
OPENCODE_MODEL=
OLLAMA_BASE_URL=[http://127.0.0.1:11434](http://127.0.0.1:11434)
OLLAMA_MODEL=qwen2.5:7b-instruct
DATABASE_PATH=data/bot.sqlite3
LOGS_DIRECTORY=logs
POST_INTERVAL_MINUTES=60
SEARCH_WINDOW_HOURS=72
QUIET_HOURS_START_HOUR=
QUIET_HOURS_END_HOUR=
MAX_POSTS_PER_RUN=2