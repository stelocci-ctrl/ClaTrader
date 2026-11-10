"""Configurazione letta da variabili d'ambiente / file .env."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .strategy import StrategyParams


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "si", "sì"}


def _required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Variabile d'ambiente mancante: {name}")
    return val


def category_from_env() -> str:
    category = os.getenv("BYBIT_CATEGORY", "linear").strip().lower()
    if category not in {"linear", "spot"}:
        raise RuntimeError("BYBIT_CATEGORY deve essere 'linear' o 'spot'")
    return category


def symbols_from_env() -> tuple[str, ...]:
    return tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip())


def strategy_params_from_env() -> StrategyParams:
    """Parametri della strategia dal .env (valori mancanti = default di StrategyParams).

    Non richiede le chiavi API: usato sia dal bot sia dal backtest.
    """
    load_dotenv()
    d = StrategyParams()
    return StrategyParams(
        ema_fast=int(os.getenv("EMA_FAST", d.ema_fast)),
        ema_slow=int(os.getenv("EMA_SLOW", d.ema_slow)),
        ema_trend=int(os.getenv("EMA_TREND", d.ema_trend)),
        rsi_period=int(os.getenv("RSI_PERIOD", d.rsi_period)),
        rsi_long_max=float(os.getenv("RSI_LONG_MAX", d.rsi_long_max)),
        rsi_short_min=float(os.getenv("RSI_SHORT_MIN", d.rsi_short_min)),
        atr_period=int(os.getenv("ATR_PERIOD", d.atr_period)),
        atr_sl_mult=float(os.getenv("ATR_SL_MULT", d.atr_sl_mult)),
        reward_risk=float(os.getenv("REWARD_RISK", d.reward_risk)),
        volume_period=int(os.getenv("VOLUME_PERIOD", d.volume_period)),
        volume_mult=float(os.getenv("VOLUME_MULT", d.volume_mult)),
    )


@dataclass(frozen=True)
class Config:
    # Bybit
    bybit_api_key: str
    bybit_api_secret: str
    bybit_testnet: bool
    category: str  # "linear" (perpetual USDT) oppure "spot"
    symbols: tuple[str, ...]
    leverage: int

    # Telegram
    telegram_token: str
    telegram_chat_id: int

    # Strategia
    interval: str  # minuti Bybit: 1,3,5,15,30,60,120,240,D
    strategy: StrategyParams

    # Rischio / esecuzione
    risk_per_trade_pct: float
    max_position_usdt: float
    approval_timeout_sec: int
    max_price_deviation_pct: float
    poll_interval_sec: int
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        category = category_from_env()
        symbols = symbols_from_env()
        return cls(
            bybit_api_key=_required("BYBIT_API_KEY"),
            bybit_api_secret=_required("BYBIT_API_SECRET"),
            bybit_testnet=_bool("BYBIT_TESTNET", True),
            category=category,
            symbols=symbols,
            leverage=int(os.getenv("LEVERAGE", "1")),
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=int(_required("TELEGRAM_CHAT_ID")),
            interval=os.getenv("INTERVAL", "15"),
            strategy=strategy_params_from_env(),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),
            max_position_usdt=float(os.getenv("MAX_POSITION_USDT", "1000")),
            approval_timeout_sec=int(os.getenv("APPROVAL_TIMEOUT_SEC", "300")),
            max_price_deviation_pct=float(os.getenv("MAX_PRICE_DEVIATION_PCT", "0.5")),
            poll_interval_sec=int(os.getenv("POLL_INTERVAL_SEC", "60")),
            dry_run=_bool("DRY_RUN", True),
        )
