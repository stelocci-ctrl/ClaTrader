"""Registro delle posizioni spot aperte DAL BOT, salvato su file.

Nello spot non esiste una "posizione": si possiedono monete. Per non confondere
le monete che hai già (o quelle regalate dal conto demo) con gli acquisti del
bot, il bot annota quanto possedevi prima dell'acquisto e quanto ha comprato.
La posizione è aperta finché il saldo resta sopra quel livello; quando scende
(stop loss, take profit o vendita manuale) viene considerata chiusa.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class SpotPositionBook:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception("File posizioni %s illeggibile: riparto da vuoto", self.path)
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def open(self, symbol: str, qty: float, base_before: float, entry: float, sl: float, tp: float) -> None:
        self._data[symbol] = {
            "qty": qty, "base_before": base_before, "entry": entry,
            "stop_loss": sl, "take_profit": tp, "opened_at": int(time.time()),
        }
        self._save()

    def get(self, symbol: str) -> dict[str, Any] | None:
        return self._data.get(symbol)

    def close(self, symbol: str) -> dict[str, Any] | None:
        rec = self._data.pop(symbol, None)
        if rec is not None:
            self._save()
        return rec

    def symbols(self) -> list[str]:
        return list(self._data)

    @staticmethod
    def is_still_open(rec: dict[str, Any], base_balance: float) -> bool:
        """Aperta finché si possiede ancora almeno metà di quanto comprato dal bot."""
        return base_balance >= rec["base_before"] + rec["qty"] * 0.5
