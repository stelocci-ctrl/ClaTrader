"""Client OKX (spot) tramite ccxt, con la stessa interfaccia di BybitClient.

- REGION=eu  -> OKX Europe (eea.okx.com, licenza MiCA), classe ccxt `myokx`
- REGION=global -> www.okx.com, classe ccxt `okx`
- testnet=True -> OKX Demo Trading (header x-simulated-trading: 1); servono
  chiavi API create dentro la Demo Trading.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import ccxt

from .risk import InstrumentRules, round_to_tick
from .strategy import Candle, Signal

log = logging.getLogger(__name__)


def to_timeframe(interval: str) -> str:
    """Converte l'INTERVAL in stile Bybit (minuti, 'D', 'W') nel timeframe ccxt ('15m', '1h', '1d')."""
    iv = interval.strip().upper()
    if iv in {"D", "1D"}:
        return "1d"
    if iv in {"W", "1W"}:
        return "1w"
    minutes = int(iv)
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def make_exchange(region: str, testnet: bool, api_key: str = "", secret: str = "", passphrase: str = ""):
    cls = ccxt.myokx if region == "eu" else ccxt.okx
    ex = cls({"apiKey": api_key, "secret": secret, "password": passphrase, "enableRateLimit": True})
    if testnet:
        ex.set_sandbox_mode(True)
    return ex


class OkxClient:
    category = "spot"

    def __init__(self, api_key: str, api_secret: str, passphrase: str, testnet: bool, region: str = "eu"):
        self.ex = make_exchange(region, testnet, api_key, api_secret, passphrase)
        self._markets_loaded = False

    # ---------------------------------------------------------------- mercati
    def _market(self, symbol: str) -> dict[str, Any]:
        """Trova il mercato spot per 'BTCUSDC', 'BTC-USDC' o 'BTC/USDC'."""
        if not self._markets_loaded:
            self.ex.load_markets()
            self._markets_loaded = True
        wanted = symbol.upper().replace("-", "").replace("/", "")
        for m in self.ex.markets.values():
            if m.get("spot") and f"{m['base']}{m['quote']}".upper() == wanted:
                return m
        raise RuntimeError(
            f"Simbolo {symbol} non disponibile su OKX spot. Usa coppie esistenti, es. BTCUSDC o BTCEUR."
        )

    def get_closed_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        """Candele chiuse, dalla più vecchia alla più recente."""
        m = self._market(symbol)
        tf = to_timeframe(interval)
        rows = self.ex.fetch_ohlcv(m["symbol"], tf, limit=min(limit + 1, 300))
        tf_ms = self.ex.parse_timeframe(tf) * 1000
        now = self.ex.milliseconds()
        return [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0))
            for r in rows
            if r[0] + tf_ms <= now  # scarta la candela ancora in formazione
        ]

    def get_last_price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(self._market(symbol)["symbol"])["last"])

    def get_rules(self, symbol: str) -> InstrumentRules:
        m = self._market(symbol)
        amount = m["limits"]["amount"]
        cost_min = (m["limits"].get("cost") or {}).get("min")
        return InstrumentRules(
            qty_step=Decimal(str(m["precision"]["amount"])),
            min_qty=Decimal(str(amount.get("min") or m["precision"]["amount"])),
            max_qty=Decimal(str(amount.get("max") or 10**12)),
            tick_size=Decimal(str(m["precision"]["price"])),
            min_notional=Decimal(str(cost_min or 0)),
        )

    def base_currency(self, symbol: str) -> str:
        return self._market(symbol)["base"]

    def quote_currency(self, symbol: str) -> str:
        return self._market(symbol)["quote"]

    # ---------------------------------------------------------------- conto
    def get_coin_balance(self, coin: str) -> float:
        bal = self.ex.fetch_balance()
        return float((bal.get("total") or {}).get(coin) or 0)

    def get_equity(self, symbol: str) -> float:
        """Saldo della valuta di quotazione (es. USDC per BTCUSDC) nel conto di trading."""
        return self.get_coin_balance(self.quote_currency(symbol))

    def get_open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol is None:
            return []
        m = self._market(symbol)
        size = self.get_coin_balance(m["base"])
        if size >= float(self.get_rules(symbol).min_qty):
            return [{"symbol": symbol, "side": "Buy", "size": str(size), "unrealisedPnl": "-"}]
        return []

    def has_open_position(self, symbol: str) -> bool:
        return bool(self.get_open_positions(symbol))

    def ensure_leverage(self, symbol: str, leverage: int) -> None:
        return None  # spot: nessuna leva

    # ---------------------------------------------------------------- ordini
    def order_params(self, signal: Signal) -> dict[str, Any]:
        """Parametri ccxt: quantità in moneta base e TP/SL agganciati (attachAlgoOrds)."""
        tick = self.get_rules(signal.symbol).tick_size
        params: dict[str, Any] = {
            "tgtCcy": "base_ccy",
            "stopLoss": {"triggerPrice": float(round_to_tick(signal.stop_loss, tick)), "type": "market"},
        }
        if signal.take_profit is not None:
            params["takeProfit"] = {"triggerPrice": float(round_to_tick(signal.take_profit, tick)), "type": "market"}
        return params

    def place_market_order(self, signal: Signal, qty: Decimal) -> dict[str, Any]:
        """Ordine a mercato con stop loss e take profit agganciati."""
        if signal.side != "Buy":
            raise RuntimeError("OKX spot: il bot apre solo posizioni LONG")
        m = self._market(signal.symbol)
        params = self.order_params(signal)
        log.info("Invio ordine OKX: %s buy %s %s", m["symbol"], qty, params)
        order = self.ex.create_order(m["symbol"], "market", "buy", float(qty), None, params)
        return {"orderId": order.get("id")}

    def cancel_protective_orders(self, symbol: str) -> int:
        """Cancella gli ordini stop/target (algo) ancora attivi sul simbolo. Ritorna quanti."""
        sym = self._market(symbol)["symbol"]
        cancelled = 0
        for ord_type in ("conditional", "oco"):
            for order in self.ex.fetch_open_orders(sym, params={"ordType": ord_type}):
                self.ex.cancel_order(order["id"], sym, params={"trigger": True})
                cancelled += 1
        return cancelled

    def sell_market(self, symbol: str, qty: Decimal) -> dict[str, Any]:
        """Vende `qty` monete base a mercato."""
        order = self.ex.create_order(self._market(symbol)["symbol"], "market", "sell", float(qty))
        return {"orderId": order.get("id")}
