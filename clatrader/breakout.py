"""Trend following "breakout" (canale di Donchian, stile Turtle), solo long.

Regole, valutate sulla chiusura di ogni candela (tipicamente giornaliera):
  ENTRATA - la chiusura supera il massimo delle `entry_period` candele precedenti
            (facoltativo: e il prezzo è sopra la media mobile `trend_sma`).
            Si entra all'APERTURA della candela successiva.
  STOP    - stop iniziale = entrata - `stop_atr` x ATR(`atr_period`); se il minimo
            della candela lo tocca si esce allo stop (o all'apertura, se apre sotto).
  USCITA  - la chiusura scende sotto il minimo delle `exit_period` candele precedenti;
            si esce all'apertura della candela successiva. Nessun target fisso.

1 R = distanza tra entrata e stop iniziale. Il risultato di ogni operazione è
espresso in R, al netto di commissioni e slippage su entrambi i lati.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .indicators import atr, sma
from .strategy import Candle


@dataclass(frozen=True)
class BreakoutParams:
    entry_period: int = 20
    exit_period: int = 10
    atr_period: int = 20
    stop_atr: float = 2.0
    trend_sma: int = 0  # 0 = nessun filtro di trend

    @property
    def warmup(self) -> int:
        return max(self.entry_period, self.exit_period, self.atr_period, self.trend_sma) + 1

    def label(self) -> str:
        trend = f", SMA{self.trend_sma}" if self.trend_sma else ""
        return f"entrata {self.entry_period}g, uscita {self.exit_period}g, stop {self.stop_atr:g}×ATR{trend}"


@dataclass
class Trade:
    entry_ms: int
    exit_ms: int
    entry: float
    exit: float
    stop: float
    r_gross: float
    r_net: float
    reason: str  # "stop" | "uscita" | "aperta"


@dataclass
class BreakoutResult:
    trades: list[Trade] = field(default_factory=list)
    buy_hold_pct: float = 0.0
    start_ms: int = 0
    end_ms: int = 0

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if t.reason != "aperta"]

    @property
    def n(self) -> int:
        return len(self.closed)

    @property
    def win_rate(self) -> float:
        return sum(t.r_net > 0 for t in self.closed) / self.n * 100 if self.n else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.r_net for t in self.closed)

    @property
    def total_r_gross(self) -> float:
        return sum(t.r_gross for t in self.closed)

    @property
    def avg_r(self) -> float:
        return self.total_r / self.n if self.n else 0.0

    @property
    def max_drawdown_r(self) -> float:
        peak = equity = dd = 0.0
        for t in self.closed:
            equity += t.r_net
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        return dd

    @property
    def max_losing_streak(self) -> int:
        worst = cur = 0
        for t in self.closed:
            cur = cur + 1 if t.r_net <= 0 else 0
            worst = max(worst, cur)
        return worst

    @property
    def best_r(self) -> float:
        return max((t.r_net for t in self.closed), default=0.0)


def entry_signal(candles: Sequence[Candle], p: BreakoutParams) -> bool:
    """True se l'ultima candela chiusa rompe il massimo delle `entry_period` precedenti."""
    if len(candles) < p.warmup + 1:
        return False
    last = candles[-1]
    prev_high = max(c.high for c in candles[-1 - p.entry_period:-1])
    if last.close <= prev_high:
        return False
    if p.trend_sma:
        trend = sma([c.close for c in candles], p.trend_sma)[-1]
        if trend is None or last.close <= trend:
            return False
    return True


def exit_signal(candles: Sequence[Candle], p: BreakoutParams) -> bool:
    """True se l'ultima chiusura scende sotto il minimo delle `exit_period` candele precedenti."""
    if len(candles) < p.exit_period + 1:
        return False
    prev_low = min(c.low for c in candles[-1 - p.exit_period:-1])
    return candles[-1].close < prev_low


def simulate(
    candles: Sequence[Candle], p: BreakoutParams, fee_pct: float = 0.1, slippage_pct: float = 0.05
) -> BreakoutResult:
    """Simula la strategia senza guardare il futuro: decisioni sulla chiusura, esecuzione dopo."""
    res = BreakoutResult()
    if len(candles) < p.warmup + 2:
        return res
    cost = (fee_pct + slippage_pct) / 100  # per lato
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atrs = atr(highs, lows, closes, p.atr_period)
    trend = sma(closes, p.trend_sma) if p.trend_sma else None

    start = p.warmup
    res.start_ms, res.end_ms = candles[start].start_ms, candles[-1].start_ms
    res.buy_hold_pct = (closes[-1] / candles[start].open - 1) * 100

    def r_values(entry: float, exit_px: float, stop: float) -> tuple[float, float]:
        risk = entry - stop
        gross = (exit_px - entry) / risk
        net = (exit_px * (1 - cost) - entry * (1 + cost)) / risk
        return gross, net

    i = start
    in_pos = False
    entry = stop = stop_init = 0.0
    entry_ms = 0
    while i < len(candles):
        c = candles[i]
        if in_pos:
            # 1) stop iniziale toccato durante la candela
            if c.low <= stop:
                px = min(c.open, stop)  # se apre sotto lo stop, si esce all'apertura
                g, n = r_values(entry, px, stop_init)
                res.trades.append(Trade(entry_ms, c.start_ms, entry, px, stop_init, g, n, "stop"))
                in_pos = False
            # 2) uscita sul canale: decisa alla chiusura, eseguita all'apertura successiva
            elif min(lows[i - p.exit_period:i]) > closes[i] and i + 1 < len(candles):
                px = candles[i + 1].open
                g, n = r_values(entry, px, stop_init)
                res.trades.append(Trade(entry_ms, candles[i + 1].start_ms, entry, px, stop_init, g, n, "uscita"))
                in_pos = False
                i += 1  # la candela successiva è quella dell'uscita
                continue
        else:
            breakout = closes[i] > max(highs[i - p.entry_period:i])
            trend_ok = not trend or (trend[i] is not None and closes[i] > trend[i])
            a = atrs[i]
            if breakout and trend_ok and a and i + 1 < len(candles):
                nxt = candles[i + 1]
                entry = nxt.open
                stop_init = stop = entry - p.stop_atr * a
                entry_ms = nxt.start_ms
                in_pos = True
                i += 1
                # lo stop può scattare già nella candela di entrata
                if nxt.low <= stop:
                    px = min(nxt.open, stop)
                    g, n = r_values(entry, px, stop_init)
                    res.trades.append(Trade(entry_ms, nxt.start_ms, entry, px, stop_init, g, n, "stop"))
                    in_pos = False
                i += 1
                continue
        i += 1

    if in_pos:
        g, n = r_values(entry, closes[-1], stop_init)
        res.trades.append(Trade(entry_ms, candles[-1].start_ms, entry, closes[-1], stop_init, g, n, "aperta"))
    return res


GRID = [
    BreakoutParams(entry_period=e, exit_period=x, stop_atr=s)
    for e in (20, 40, 55)
    for x in (10, 20)
    for s in (2.0, 3.0)
]
