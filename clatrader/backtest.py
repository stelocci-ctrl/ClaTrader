"""Backtest della strategia su dati pubblici Bybit o OKX (non servono chiavi API).

Usa gli stessi parametri del bot letti dal .env (EXCHANGE, REGION, SYMBOLS, INTERVAL, CATEGORY,
EMA_*, RSI_*, ATR_*, REWARD_RISK, VOLUME_*). Gli argomenti da riga di comando
hanno la precedenza:

    python -m clatrader.backtest                       # tutto dal .env
    python -m clatrader.backtest -s breakout           # trend following giornaliero + robustezza
    python -m clatrader.backtest BTCUSDT -i 60 -n 5000
    VOLUME_MULT=0 python -m clatrader.backtest         # sovrascrive una variabile
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from .config import (
    EXCHANGES,
    REGION_TLD,
    category_from_env,
    exchange_from_env,
    region_from_env,
    strategy_params_from_env,
    symbols_from_env,
)
from .strategy import Candle, StrategyParams, describe, evaluate

# commissione taker per lato, in %
FEES_PCT = {"linear": 0.055, "spot": 0.1}


@dataclass
class Result:
    trades: int = 0
    wins: int = 0
    total_r: float = 0.0


def fetch(symbol: str, interval: str, n: int, category: str = "linear", tld: str = "com") -> list[Candle]:
    s = HTTP(testnet=False, tld=tld)
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


def fetch_okx(symbol: str, interval: str, n: int, region: str = "eu") -> list[Candle]:
    """Candele storiche OKX spot (dati pubblici reali, non della Demo)."""
    from .okx import OkxClient, to_timeframe

    client = OkxClient("", "", "", testnet=False, region=region)
    ex = client.ex
    market = client._market(symbol)
    tf = to_timeframe(interval)
    tf_ms = ex.parse_timeframe(tf) * 1000
    now = ex.milliseconds()
    rows = ex.fetch_ohlcv(
        market["symbol"], tf, since=now - (n + 1) * tf_ms,
        params={"paginate": True, "paginationCalls": n // 100 + 5},
    )
    seen: dict[int, Candle] = {}
    for r in rows:
        if r[0] + tf_ms <= now:  # solo candele chiuse
            seen[int(r[0])] = Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0))
    return [seen[k] for k in sorted(seen)][-n:]


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


def _date(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d/%m/%Y")


def report_breakout(symbol: str, candles: list[Candle], fee: float, slippage: float) -> None:
    from .breakout import GRID, BreakoutParams, simulate

    base = simulate(candles, BreakoutParams(), fee, slippage)
    print(f"{symbol}: {len(candles)} candele dal {_date(base.start_ms)} al {_date(base.end_ms)}")
    print(f"  Parametri base ({BreakoutParams().label()}):")
    print(f"  operazioni {base.n} · vinte {base.win_rate:.0f}% · media {base.avg_r:+.2f} R · "
          f"totale {base.total_r:+.1f} R (prima dei costi {base.total_r_gross:+.1f} R)")
    print(f"  drawdown max {base.max_drawdown_r:.1f} R · peggior serie {base.max_losing_streak} perdite · "
          f"miglior operazione {base.best_r:+.1f} R")
    print(f"  buy & hold nello stesso periodo: {base.buy_hold_pct:+.0f}% · stop medio {base.avg_stop_pct:.1f}% sotto l'entrata")
    risk = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
    for rk in sorted({1.0, risk}):
        for lev in (1.0, 5.0):
            ret, dd = base.compounded(rk, lev)
            print(f"  rischio {rk:g}% · {'senza leva' if lev == 1 else f'leva max {lev:g}x'}: "
                  f"rendimento {ret:+.0f}% · drawdown max -{dd:.0f}%")
    if base.trades and base.trades[-1].reason == "aperta":
        print(f"  (operazione ancora aperta: {base.trades[-1].r_net:+.1f} R, non conteggiata)")
    if base.n < 30:
        print("  ⚠ meno di 30 operazioni: risultato poco significativo.")

    print("  Robustezza (media R/operazione · numero operazioni):")
    results = [(p, simulate(candles, p, fee, slippage)) for p in GRID]
    for p, r in results:
        mark = "+" if r.avg_r > 0 else "-"
        print(f"    {mark} {p.label():38s} {r.avg_r:+.2f} R · {r.n:3d} op. · DD {r.max_drawdown_r:4.1f} R")
    positive = sum(r.avg_r > 0 for _, r in results)
    print(f"  → positive {positive} su {len(results)} combinazioni")
    print()


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Backtest ClaTrader con i parametri del .env")
    ap.add_argument("symbols", nargs="*", help="simboli (default: SYMBOLS del .env)")
    ap.add_argument("-i", "--interval", default=None, help="timeframe: minuti (15, 60, 240) o D")
    ap.add_argument("-n", "--candles", type=int, default=None, help="numero di candele")
    ap.add_argument("-c", "--category", choices=["linear", "spot"], default=None)
    ap.add_argument("-r", "--region", choices=list(REGION_TLD), default=None, help="global o eu")
    ap.add_argument("-e", "--exchange", choices=list(EXCHANGES), default=None, help="bybit o okx")
    ap.add_argument("-s", "--strategy", choices=["ema", "breakout"], default="ema",
                    help="ema = strategia del bot | breakout = trend following (default TF giornaliero)")
    ap.add_argument("--slippage", type=float, default=0.05, help="slippage %% per lato (breakout)")
    args = ap.parse_args()
    breakout = args.strategy == "breakout"
    if args.interval is None:
        args.interval = "D" if breakout else os.getenv("INTERVAL", "15")
    if args.candles is None:
        args.candles = 2000 if breakout else 3000

    exchange = args.exchange or exchange_from_env()
    region = args.region or region_from_env()
    category = "spot" if exchange == "okx" else (args.category or category_from_env())
    symbols = [s.upper() for s in args.symbols] or list(symbols_from_env())
    params = strategy_params_from_env()
    fee = FEES_PCT[category]

    if args.strategy == "breakout":
        print("Strategia: trend following breakout (Donchian), solo long")
    else:
        print(f"Strategia: {describe(params)}")
    print(f"Mercato: {exchange.upper()} {'EU' if region == 'eu' else 'global'} {category} · TF {args.interval} · fee {fee}% per lato"
          + (" · solo long" if category == "spot" else ""))
    print()
    for symbol in symbols:
        if exchange == "okx":
            candles = fetch_okx(symbol, args.interval, args.candles, region)
        else:
            candles = fetch(symbol, args.interval, args.candles, category, REGION_TLD[region])
        if args.strategy == "breakout":
            report_breakout(symbol, candles, fee, args.slippage)
            continue
        r = run(candles, params, fee, allow_short=category != "spot")
        wr = r.wins / r.trades * 100 if r.trades else 0
        avg = r.total_r / r.trades if r.trades else 0
        print(f"{symbol}: {len(candles)} candele · trade {r.trades} · win rate {wr:.1f}% · "
              f"totale {r.total_r:+.2f} R · media {avg:+.3f} R/trade (fee incluse)")
        if r.trades < 30:
            print("  ⚠ meno di 30 trade: risultato poco significativo.")


if __name__ == "__main__":
    main()
