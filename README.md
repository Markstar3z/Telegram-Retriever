# Project Admin Retriever

A Telegram bot that accepts X/Twitter project links paired with Telegram group links and reports the group owner plus recently active human administrators.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`, then replace every placeholder with your own Telegram credentials. Obtain `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org/apps), and create the bot token through [@BotFather](https://t.me/BotFather).
4. Load the environment variables before starting the app. In PowerShell:

   ```powershell
   $env:API_ID = "12345678"
   $env:API_HASH = "your_api_hash"
   $env:BOT_TOKEN = "your_bot_token"
   python Retriever.py
   ```

The first run authenticates your personal Telegram account and creates a local session file. Keep that file private; it is ignored by Git.

## GitHub upload checklist

- Regenerate the bot token and API hash that were previously embedded in the script.
- Confirm `.env` and all `*.session` files are not staged.
- Create a repository on GitHub, then commit and push this folder using GitHub Desktop or Git after it is installed.

This repository intentionally contains only source code and configuration templates, never live credentials or Telegram session data.
