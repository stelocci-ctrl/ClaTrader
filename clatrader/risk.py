"""Dimensionamento della posizione e arrotondamenti agli step dell'exchange."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class InstrumentRules:
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    tick_size: Decimal
    min_notional: Decimal = Decimal("0")


def round_down_to_step(value: float | Decimal, step: Decimal) -> Decimal:
    v = Decimal(str(value))
    return (v / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_to_tick(price: float | Decimal, tick: Decimal) -> Decimal:
    p = Decimal(str(price))
    return ((p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick).normalize()


def position_size(
    equity: float,
    risk_pct: float,
    entry: float,
    stop_distance: float,
    max_position_value: float,
    rules: InstrumentRules,
) -> Decimal:
    """Quantità tale che, se viene colpito lo stop, si perde `risk_pct`% dell'equity.

    La quantità è limitata da `max_position_value` (in USDT) e arrotondata per
    difetto allo step dell'exchange. Ritorna 0 se non si raggiunge il minimo.
    """
    if equity <= 0 or entry <= 0 or stop_distance <= 0 or risk_pct <= 0:
        return Decimal("0")
    risk_amount = equity * risk_pct / 100.0
    qty = risk_amount / stop_distance
    qty = min(qty, max_position_value / entry)
    q = round_down_to_step(qty, rules.qty_step)
    if q > rules.max_qty:
        q = round_down_to_step(rules.max_qty, rules.qty_step)
    if q < rules.min_qty:
        return Decimal("0")
    if rules.min_notional and q * Decimal(str(entry)) < rules.min_notional:
        return Decimal("0")
    return q.normalize()
