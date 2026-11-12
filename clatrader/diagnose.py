"""Diagnosi degli ordini su Bybit (SOLO TESTNET).

Prova alcune varianti di ordine per capire quale viene rifiutata, senza
comprare nulla: gli ordini limite sono piazzati al 80% del prezzo attuale
(quindi non vengono eseguiti) e cancellati subito.

Uso:  python -m clatrader.diagnose [SIMBOLO]
"""
from __future__ import annotations

import sys
from decimal import ROUND_UP, Decimal

from pybit.exceptions import InvalidRequestError

from .config import Config
from .exchange import BybitClient
from .risk import round_to_tick


def _min_qty(rules, price: Decimal) -> Decimal:
    """Quantità minima accettata, anche rispetto al controvalore minimo."""
    qty = rules.min_qty
    if rules.min_notional and qty * price < rules.min_notional:
        qty = (rules.min_notional * Decimal("1.2") / price / rules.qty_step).to_integral_value(ROUND_UP) * rules.qty_step
    return qty.normalize()


def _try(client: BybitClient, name: str, params: dict) -> bool:
    try:
        res = client.session.place_order(**params)
    except InvalidRequestError as e:
        print(f"❌ {name}: RIFIUTATO (codice {e.status_code}) {e.message[:160]}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"❌ {name}: ERRORE {e}")
        return False
    order_id = res["result"].get("orderId")
    print(f"✅ {name}: ACCETTATO")
    try:
        client.session.cancel_order(category=params["category"], symbol=params["symbol"], orderId=order_id)
    except Exception as e:  # noqa: BLE001
        print(f"   ⚠️ cancellazione non riuscita, annullalo a mano dal sito: {e}")
    return True


def main() -> None:
    cfg = Config.from_env()
    if cfg.exchange != "bybit":
        sys.exit("La diagnosi degli ordini è disponibile solo per Bybit.")
    if not cfg.testnet:
        sys.exit("Per sicurezza la diagnosi funziona solo con TESTNET=true.")
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else cfg.symbols[0]
    client = BybitClient(cfg.api_key, cfg.api_secret, True, cfg.category, cfg.tld)

    rules = client.get_rules(symbol)
    price = Decimal(str(client.get_last_price(symbol)))
    limit = round_to_tick(price * Decimal("0.8"), rules.tick_size)
    qty = _min_qty(rules, limit)
    print(f"Bybit {'EU' if cfg.region == 'eu' else 'global'} testnet · {cfg.category} · {symbol}")
    print(f"Prezzo {price} · ordine limite di prova a {limit} (non verrà eseguito) · quantità {qty}\n")

    base = {
        "category": cfg.category, "symbol": symbol, "side": "Buy", "orderType": "Limit",
        "qty": str(qty), "price": str(limit), "timeInForce": "GTC",
    }
    tp = str(round_to_tick(limit * Decimal("1.1"), rules.tick_size))
    sl = str(round_to_tick(limit * Decimal("0.9"), rules.tick_size))
    plain = _try(client, "1. Ordine limite semplice", base)
    _try(client, "2. Ordine limite con TP/SL agganciati", {**base, "takeProfit": tp, "stopLoss": sl})
    if cfg.category == "spot":
        _try(client, "3. Ordine condizionale TP/SL separato", {
            **base, "side": "Sell", "price": str(round_to_tick(price * Decimal("0.7"), rules.tick_size)),
            "triggerPrice": str(round_to_tick(price * Decimal("0.75"), rules.tick_size)),
            "orderFilter": "tpslOrder",
        })

    print()
    if not plain:
        print("Anche l'ordine semplice è rifiutato: il conto o la chiave API non possono fare trading.\n"
              "Controlla su Bybit la verifica dell'identità (KYC) e i permessi della chiave (Trade).")
    else:
        print("Copia queste righe e mandale a Claude.")


if __name__ == "__main__":
    main()
