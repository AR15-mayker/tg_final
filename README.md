# tg_final
Telegram Bot Calendar

This bot serves as a personal event calendar with reminders, group moderation features,
and content delivery to a specified channel.

Features:
- Private Chat:
  - Add events via an interactive calendar (/calendar).
  - View, manage, and delete events (/myevents).
  - Set one-time reminders for events.
  - Clear all personal events (/clearevents).
- Group Chat:
  - Automatic deletion of messages containing forbidden words.
  - Fun commands like /play (dice).
- Channel Management:
  - Admins can post messages to a configured channel (/post).
  - Automatic daily posts with a random quote.
- General Features:
  - Fetches random quotes and images from external APIs.
  - FSM for multi-step dialogues (setting reminders).
  - Comprehensive logging using Loguru.
  - Asynchronous database operations with aiosqlite.
  - Robust error handling and background tasks for reminders and daily posts.

Setup:
1.  Install dependencies:
    pip install aiogram aiosqlite aiohttp python-dotenv loguru aiogram-calendar
2.  Create a .env file with the following variables:
    - TOKEN: Your Telegram bot token.
    - ADMIN_USER_IDS: Comma-separated list of admin user IDs.
    - TARGET_CHANNEL_ID: The ID of the channel for daily posts.
    - STICKER_ID: The file_id for the sticker used in /sticker. (Optional)
"""
