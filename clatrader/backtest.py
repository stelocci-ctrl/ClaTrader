"""Backtest semplice della strategia su dati pubblici Bybit (non servono chiavi API).

Uso:  python -m clatrader.backtest BTCUSDT 15 1000
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from pybit.unified_trading import HTTP

from .strategy import Candle, StrategyParams, evaluate

FEE_PCT = 0.055  # taker Bybit per lato (perpetual)


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


def run(candles: list[Candle], p: StrategyParams) -> Result:
    res = Result()
    i = p.min_candles
    while i < len(candles):
        sig = evaluate("X", candles[: i + 1], p)
        if not sig:
            i += 1
            continue
        fee_r = sig.entry * FEE_PCT / 100 * 2 / sig.stop_distance
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
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "15"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
    candles = fetch(symbol, interval, n)
    r = run(candles, StrategyParams())
    wr = r.wins / r.trades * 100 if r.trades else 0
    print(f"{symbol} TF {interval} su {len(candles)} candele")
    print(f"Trade: {r.trades} · Win rate: {wr:.1f}% · Totale: {r.total_r:+.2f} R (fee incluse)")
    if r.trades < 30:
        print("Attenzione: meno di 30 trade, risultato poco significativo.")


if __name__ == "__main__":
    main()
