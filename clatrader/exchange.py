"""Wrapper sottile attorno all'API Bybit v5 (pybit.unified_trading)."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

from .risk import InstrumentRules, round_to_tick
from .strategy import Candle, Signal

log = logging.getLogger(__name__)

LEVERAGE_NOT_MODIFIED = 110043


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool, category: str, tld: str = "com"):
        self.category = category
        # tld="eu" -> api(-testnet).bybit.eu (Bybit EU), "com" -> bybit.com
        self.session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret, tld=tld)
        self._rules: dict[str, InstrumentRules] = {}
        self._base_coin: dict[str, str] = {}
        self._quote_coin: dict[str, str] = {}

    # ---------------------------------------------------------------- mercato
    def get_closed_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        """Candele chiuse, dalla più vecchia alla più recente."""
        res = self.session.get_kline(
            category=self.category, symbol=symbol, interval=interval, limit=min(limit + 1, 1000)
        )
        rows = res["result"]["list"]  # Bybit le restituisce dalla più recente
        candles = [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
            for r in reversed(rows)
        ]
        # l'ultima candela è quella in formazione: la scartiamo
        return candles[:-1]

    def get_last_price(self, symbol: str) -> float:
        res = self.session.get_tickers(category=self.category, symbol=symbol)
        return float(res["result"]["list"][0]["lastPrice"])

    def get_rules(self, symbol: str) -> InstrumentRules:
        if symbol in self._rules:
            return self._rules[symbol]
        res = self.session.get_instruments_info(category=self.category, symbol=symbol)
        if not res["result"]["list"]:
            raise RuntimeError(
                f"Simbolo {symbol} non disponibile su Bybit ({self.category}). "
                "Su Bybit EU usa coppie in USDC o EUR, es. BTCUSDC."
            )
        info = res["result"]["list"][0]
        lot = info["lotSizeFilter"]
        if self.category == "spot":
            step = lot.get("basePrecision") or lot.get("qtyStep")
            min_notional = lot.get("minOrderAmt", "0")
        else:
            step = lot["qtyStep"]
            min_notional = lot.get("minNotionalValue", "0")
        rules = InstrumentRules(
            qty_step=Decimal(step),
            min_qty=Decimal(lot["minOrderQty"]),
            max_qty=Decimal(lot.get("maxMktOrderQty") or lot["maxOrderQty"]),
            tick_size=Decimal(info["priceFilter"]["tickSize"]),
            min_notional=Decimal(min_notional or "0"),
        )
        self._rules[symbol] = rules
        self._base_coin[symbol] = info.get("baseCoin", symbol.removesuffix("USDT"))
        self._quote_coin[symbol] = info.get("quoteCoin") or info.get("settleCoin") or "USDT"
        return rules

    def base_currency(self, symbol: str) -> str:
        self.get_rules(symbol)
        return self._base_coin[symbol]

    def quote_currency(self, symbol: str) -> str:
        """Valuta in cui sono espressi prezzi, saldo e rischio (USDT, USDC, EUR...)."""
        self.get_rules(symbol)
        if self.category == "linear":
            return "USD"  # totalEquity del conto unificato è in USD
        return self._quote_coin[symbol]

    # ---------------------------------------------------------------- account
    def get_equity(self, symbol: str) -> float:
        """Capitale su cui calcolare il rischio, nella valuta di `quote_currency(symbol)`.

        Spot: saldo della valuta di quotazione (es. USDC per BTCUSDC).
        Linear: equity totale del conto unificato.
        """
        if self.category == "spot":
            return self.get_coin_balance(self.quote_currency(symbol))
        res = self.session.get_wallet_balance(accountType="UNIFIED")
        return float(res["result"]["list"][0].get("totalEquity") or 0)

    def get_coin_balance(self, coin: str) -> float:
        res = self.session.get_wallet_balance(accountType="UNIFIED", coin=coin)
        for c in res["result"]["list"][0].get("coin", []):
            if c["coin"] == coin:
                return float(c.get("walletBalance") or 0)
        return 0.0

    def get_open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.category == "spot":
            out = []
            for sym in [symbol] if symbol else list(self._rules):
                rules = self.get_rules(sym)
                bal = self.get_coin_balance(self._base_coin[sym])
                if bal >= float(rules.min_qty):
                    out.append({"symbol": sym, "side": "Buy", "size": str(bal), "unrealisedPnl": "-"})
            return out
        params: dict[str, Any] = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
        else:
            params["settleCoin"] = "USDT"
        res = self.session.get_positions(**params)
        return [p for p in res["result"]["list"] if float(p.get("size") or 0) > 0]

    def has_open_position(self, symbol: str) -> bool:
        return bool(self.get_open_positions(symbol))

    def ensure_leverage(self, symbol: str, leverage: int) -> None:
        if self.category != "linear":
            return
        try:
            self.session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except InvalidRequestError as e:
            if getattr(e, "status_code", None) != LEVERAGE_NOT_MODIFIED:
                raise

    # ---------------------------------------------------------------- ordini
    def place_market_order(self, signal: Signal, qty: Decimal) -> dict[str, Any]:
        """Ordine a mercato con stop loss e take profit allegati."""
        rules = self.get_rules(signal.symbol)
        params: dict[str, Any] = {
            "category": self.category,
            "symbol": signal.symbol,
            "side": signal.side,
            "orderType": "Market",
            "qty": str(qty),
            "takeProfit": str(round_to_tick(signal.take_profit, rules.tick_size)),
            "stopLoss": str(round_to_tick(signal.stop_loss, rules.tick_size)),
        }
        if self.category == "linear":
            params.update(positionIdx=0, tpslMode="Full", tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice")
        else:
            params["marketUnit"] = "baseCoin"
        log.info("Invio ordine: %s", params)
        res = self.session.place_order(**params)
        return res["result"]


def make_client(cfg):
    """Crea il client dell'exchange scelto in configurazione (EXCHANGE=bybit|okx)."""
    if cfg.exchange == "okx":
        from .okx import OkxClient

        return OkxClient(cfg.api_key, cfg.api_secret, cfg.api_passphrase, cfg.testnet, cfg.region)
    return BybitClient(cfg.api_key, cfg.api_secret, cfg.testnet, cfg.category, cfg.tld)
