"""Alertes Telegram (facultatives). Une erreur réseau n'interrompt jamais le trading."""
from __future__ import annotations

import threading
import urllib.parse
import urllib.request

from .config import NotifyConfig


class Notifier:
    def __init__(self, cfg: NotifyConfig):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.telegram_token and self.cfg.telegram_chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def _post(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.cfg.telegram_chat_id, "text": f"🔵 Miuo\n{text}"}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10).read()
        except Exception:
            pass

