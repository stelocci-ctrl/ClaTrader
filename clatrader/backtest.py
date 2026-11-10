"""Backtest della strategia su dati pubblici Bybit (non servono chiavi API).

Usa gli stessi parametri del bot letti dal .env (SYMBOLS, INTERVAL, BYBIT_CATEGORY,
EMA_*, RSI_*, ATR_*, REWARD_RISK, VOLUME_*). Gli argomenti da riga di comando
hanno la precedenza:

    python -m clatrader.backtest                       # tutto dal .env
    python -m clatrader.backtest BTCUSDT -i 60 -n 5000
    VOLUME_MULT=0 python -m clatrader.backtest         # sovrascrive una variabile
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from .config import category_from_env, strategy_params_from_env, symbols_from_env
from .strategy import Candle, StrategyParams, describe, evaluate

# commissione taker per lato, in %
FEES_PCT = {"linear": 0.055, "spot": 0.1}


@dataclass
class Result:
    trades: int = 0
    wins: int = 0
    total_r: float = 0.0


def fetch(symbol: str, interval: str, n: int, category: str = "linear") -> list[Candle]:
    s = HTTP(testnet=False)
    out: list[Candle] = []
    end = None
    while len(out) < n:
        kw = {"category": category, "symbol": symbol, "interval": interval, "limit": 1000}
        if end:
            kw["end"] = end
        rows = s.get_kline(**kw)["result"]["list"]
        if not rows:
            break
        batch = [Candle(int(r[0]), *map(float, r[1:6])) for r in rows]
        out = list(reversed(batch)) + out
        end = int(rows[-1][0]) - 1
    return out[-n:][:-1]  # scarta la candela in formazione


def run(
    candles: list[Candle], p: StrategyParams, fee_pct: float = FEES_PCT["linear"], allow_short: bool = True
) -> Result:
    res = Result()
    i = p.min_candles
    while i < len(candles):
        sig = evaluate("X", candles[: i + 1], p)
        if not sig or (sig.side == "Sell" and not allow_short):
            i += 1
            continue
        fee_r = sig.entry * fee_pct / 100 * 2 / sig.stop_distance
        j = i + 1
        outcome = None
        while j < len(candles) and outcome is None:
            c = candles[j]
            if sig.side == "Buy":
                if c.low <= sig.stop_loss:
                    outcome = -1.0
                elif c.high >= sig.take_profit:
                    outcome = p.reward_risk
            else:
                if c.high >= sig.stop_loss:
                    outcome = -1.0
                elif c.low <= sig.take_profit:
                    outcome = p.reward_risk
            j += 1
        if outcome is None:
            break
        res.trades += 1
        res.wins += outcome > 0
        res.total_r += outcome - fee_r
        i = j
    return res


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Backtest ClaTrader con i parametri del .env")
    ap.add_argument("symbols", nargs="*", help="simboli (default: SYMBOLS del .env)")
    ap.add_argument("-i", "--interval", default=os.getenv("INTERVAL", "15"), help="timeframe Bybit")
    ap.add_argument("-n", "--candles", type=int, default=3000, help="numero di candele")
    ap.add_argument("-c", "--category", choices=["linear", "spot"], default=None)
    args = ap.parse_args()

    category = args.category or category_from_env()
    symbols = [s.upper() for s in args.symbols] or list(symbols_from_env())
    params = strategy_params_from_env()
    fee = FEES_PCT[category]

    print(f"Strategia: {describe(params)}")
    print(f"Mercato: {category} · TF {args.interval} · fee {fee}% per lato"
          + (" · solo long" if category == "spot" else ""))
    print()
    for symbol in symbols:
        candles = fetch(symbol, args.interval, args.candles, category)
        r = run(candles, params, fee, allow_short=category != "spot")
        wr = r.wins / r.trades * 100 if r.trades else 0
        avg = r.total_r / r.trades if r.trades else 0
        print(f"{symbol}: {len(candles)} candele · trade {r.trades} · win rate {wr:.1f}% · "
              f"totale {r.total_r:+.2f} R · media {avg:+.3f} R/trade (fee incluse)")
        if r.trades < 30:
            print("  ⚠ meno di 30 trade: risultato poco significativo.")


if __name__ == "__main__":
    main()
