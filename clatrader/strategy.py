"""Strategia trend-following: incrocio EMA con filtro di trend, RSI e stop su ATR.

Regole (valutate solo su candele CHIUSE):
  LONG  - EMA veloce incrocia sopra la EMA lenta
          - prezzo sopra la EMA di trend (es. 200)
          - RSI < RSI_LONG_MAX (evita di comprare in ipercomprato)
  SHORT - EMA veloce incrocia sotto la EMA lenta
          - prezzo sotto la EMA di trend
          - RSI > RSI_SHORT_MIN (evita di vendere in ipervenduto)
  VOLUME - (entrambi i lati) il volume della candela del segnale deve essere
          almeno VOLUME_MULT volte la media dei VOLUME_PERIOD volumi precedenti:
          un incrocio senza partecipazione del mercato viene scartato.
          VOLUME_MULT = 0 disattiva il filtro.
  Stop loss  = entry -/+ ATR * ATR_SL_MULT
  Take profit = entry +/- distanza_stop * REWARD_RISK
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence

from .indicators import atr, ema, rsi, sma

Side = Literal["Buy", "Sell"]


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    symbol: str
    side: Side
    entry: float
    stop_loss: float
    take_profit: float | None  # None = nessun target fisso (es. trend following)
    candle_start_ms: int
    reason: str
    indicators: dict[str, float] = field(default_factory=dict)

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop_loss)


@dataclass(frozen=True)
class StrategyParams:
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 200
    rsi_period: int = 14
    rsi_long_max: float = 70.0
    rsi_short_min: float = 30.0
    atr_period: int = 14
    atr_sl_mult: float = 1.5
    reward_risk: float = 2.0
    volume_period: int = 20
    volume_mult: float = 1.5

    @property
    def min_candles(self) -> int:
        return max(self.ema_trend, self.ema_slow, self.rsi_period, self.atr_period, self.volume_period + 1) + 2


@dataclass
class Analysis:
    """Stato degli indicatori sull'ultima candela chiusa e, se c'è, il segnale."""

    ready: bool = False  # False se mancano candele per calcolare gli indicatori
    price: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_trend: float = 0.0
    rsi: float = 0.0
    atr: float = 0.0
    rel_volume: float = 0.0
    crossed_up: bool = False
    crossed_down: bool = False
    missing_long: list[str] = field(default_factory=list)  # condizioni LONG non soddisfatte
    missing_short: list[str] = field(default_factory=list)
    signal: Signal | None = None


def analyze(symbol: str, candles: Sequence[Candle], p: StrategyParams) -> Analysis:
    """Calcola gli indicatori sull'ultima candela chiusa e verifica le regole.

    `candles` deve essere ordinato dal più vecchio al più recente e contenere
    solo candele chiuse.
    """
    if len(candles) < p.min_candles:
        return Analysis()

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    fast = ema(closes, p.ema_fast)
    slow = ema(closes, p.ema_slow)
    trend = ema(closes, p.ema_trend)
    r = rsi(closes, p.rsi_period)
    a = atr(highs, lows, closes, p.atr_period)
    # media dei volumi PRECEDENTI (esclude la candela del segnale)
    vol_avg = sma([c.volume for c in candles], p.volume_period)

    i = len(candles) - 1
    values = (fast[i], slow[i], fast[i - 1], slow[i - 1], trend[i], r[i], a[i])
    if any(v is None for v in values):
        return Analysis()
    f_now, s_now, f_prev, s_prev, t_now, rsi_now, atr_now = values  # type: ignore[misc]

    price = closes[i]
    volume_now = candles[i].volume
    avg_prev = vol_avg[i - 1]
    rel_volume = volume_now / avg_prev if avg_prev else 0.0
    volume_ok = p.volume_mult <= 0 or (avg_prev is not None and rel_volume >= p.volume_mult)
    stop_dist = atr_now * p.atr_sl_mult

    res = Analysis(
        ready=True,
        price=price,
        ema_fast=f_now,
        ema_slow=s_now,
        ema_trend=t_now,
        rsi=rsi_now,
        atr=atr_now,
        rel_volume=rel_volume,
        crossed_up=f_prev <= s_prev and f_now > s_now,
        crossed_down=f_prev >= s_prev and f_now < s_now,
    )

    common = []
    if stop_dist <= 0:
        common.append("ATR zero: prezzo fermo, impossibile calcolare lo stop")
    if not volume_ok:
        common.append(f"volume {rel_volume:.1f}× la media (serve ≥ {p.volume_mult:g}×)")

    res.missing_long = list(common)
    if not res.crossed_up:
        res.missing_long.insert(0, f"nessun incrocio EMA{p.ema_fast} sopra EMA{p.ema_slow} su questa candela")
    if price <= t_now:
        res.missing_long.append(f"prezzo sotto EMA{p.ema_trend} (trend non rialzista)")
    if rsi_now >= p.rsi_long_max:
        res.missing_long.append(f"RSI {rsi_now:.1f} ≥ {p.rsi_long_max:g} (ipercomprato)")

    res.missing_short = list(common)
    if not res.crossed_down:
        res.missing_short.insert(0, f"nessun incrocio EMA{p.ema_fast} sotto EMA{p.ema_slow} su questa candela")
    if price >= t_now:
        res.missing_short.append(f"prezzo sopra EMA{p.ema_trend} (trend non ribassista)")
    if rsi_now <= p.rsi_short_min:
        res.missing_short.append(f"RSI {rsi_now:.1f} ≤ {p.rsi_short_min:g} (ipervenduto)")

    ind = {
        "ema_fast": f_now,
        "ema_slow": s_now,
        "ema_trend": t_now,
        "rsi": rsi_now,
        "atr": atr_now,
        "volume": volume_now,
        "rel_volume": rel_volume,
    }
    vol_txt = f", volume {rel_volume:.1f}× la media" if p.volume_mult > 0 else ""

    if not res.missing_long:
        res.signal = Signal(
            symbol=symbol,
            side="Buy",
            entry=price,
            stop_loss=price - stop_dist,
            take_profit=price + stop_dist * p.reward_risk,
            candle_start_ms=candles[i].start_ms,
            reason=f"EMA{p.ema_fast} ha incrociato sopra EMA{p.ema_slow}, prezzo sopra EMA{p.ema_trend}{vol_txt}",
            indicators=ind,
        )
    elif not res.missing_short:
        res.signal = Signal(
            symbol=symbol,
            side="Sell",
            entry=price,
            stop_loss=price + stop_dist,
            take_profit=price - stop_dist * p.reward_risk,
            candle_start_ms=candles[i].start_ms,
            reason=f"EMA{p.ema_fast} ha incrociato sotto EMA{p.ema_slow}, prezzo sotto EMA{p.ema_trend}{vol_txt}",
            indicators=ind,
        )
    return res


def evaluate(symbol: str, candles: Sequence[Candle], p: StrategyParams) -> Signal | None:
    """Ritorna un Signal se l'ultima candela chiusa genera un ingresso, altrimenti None."""
    return analyze(symbol, candles, p).signal


def describe(p: StrategyParams) -> str:
    vol = f"volume ≥ {p.volume_mult:g}× media {p.volume_period}" if p.volume_mult > 0 else "filtro volume off"
    return (
        f"EMA {p.ema_fast}/{p.ema_slow} + trend EMA {p.ema_trend}, RSI {p.rsi_period} "
        f"(long < {p.rsi_long_max:g}, short > {p.rsi_short_min:g}), {vol}, "
        f"SL {p.atr_sl_mult:g}×ATR {p.atr_period}, R:R {p.reward_risk:g}"
    )
