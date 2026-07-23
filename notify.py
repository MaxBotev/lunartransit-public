# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 MaxBotev
"""
Telegram push notifications via the Bot API (stdlib only).

Credentials live in config.json (telegram_bot_token / telegram_chat_id) on the Pi
only -- never in the repo, and redacted from the HTTP API. Get them by:
  1. Create a bot with @BotFather -> bot token "123456:ABC...".
  2. Message your new bot once, then read the chat id (e.g. via @userinfobot,
     or GET https://api.telegram.org/bot<token>/getUpdates).
"""

import json
import ssl
import threading
import urllib.parse
import urllib.request

# Windows' root-CA store is populated lazily and fresh Python installs can't
# verify some chains; prefer certifi's bundle when present (no-op on the Pi).
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()

    def enabled(self):
        return bool(self.token and self.chat_id)

    def send(self, text):
        """Blocking send. Returns (ok, info)."""
        if not self.enabled():
            return False, "telegram not configured"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:
                j = json.loads(r.read().decode())
            return bool(j.get("ok")), j
        except Exception as e:
            return False, str(e)

    def send_async(self, text):
        threading.Thread(target=self.send, args=(text,), daemon=True).start()
