"""
telegram_notify.py
Optional Telegram notifications. Silently no-ops if TELEGRAM_TOKEN is not set.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')


class TelegramNotify:
    def __init__(self):
        self.enabled = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
        if self.enabled:
            logger.info("Telegram notifications enabled")
        else:
            logger.info("Telegram notifications disabled (TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set)")

    def send(self, message: str):
        if not self.enabled:
            logger.info(f"[NOTIFY] {message}")
            return
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            resp = requests.post(url, json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
            }, timeout=10)
            if not resp.ok:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text}")
        except Exception as exc:
            logger.warning(f"Telegram error: {exc}")
