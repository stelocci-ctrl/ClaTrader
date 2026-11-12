"""Configurazione letta da variabili d'ambiente / file .env."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .breakout import BreakoutParams
from .strategy import StrategyParams


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "si", "sì"}


def _first(*names: str, default: str = "") -> str:
    """Valore della prima variabile definita (per nomi nuovi con fallback ai vecchi BYBIT_*)."""
    for n in names:
        val = os.getenv(n)
        if val is not None and val.strip():
            return val.strip()
    return default


def _required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Variabile d'ambiente mancante: {name}")
    return val


EXCHANGES = ("bybit", "okx")

# Regione: "global" oppure "eu". Per Bybit: bybit.com / bybit.eu (servizi separati,
# chiavi diverse). Per OKX: www.okx.com / eea.okx.com (OKX Europe, MiCA).
REGION_TLD = {"global": "com", "eu": "eu"}


def exchange_from_env() -> str:
    exchange = _first("EXCHANGE", default="bybit").lower()
    if exchange not in EXCHANGES:
        raise RuntimeError(f"EXCHANGE deve essere uno tra: {', '.join(EXCHANGES)}")
    return exchange


def region_from_env() -> str:
    region = _first("REGION", "BYBIT_REGION", default="global").lower()
    if region not in REGION_TLD:
        raise RuntimeError("REGION deve essere 'global' oppure 'eu'")
    return region


def category_from_env() -> str:
    if exchange_from_env() == "okx":
        return "spot"  # il client OKX gestisce solo lo spot
    category = _first("CATEGORY", "BYBIT_CATEGORY", default="linear").lower()
    if category not in {"linear", "spot"}:
        raise RuntimeError("CATEGORY deve essere 'linear' o 'spot'")
    return category


def symbols_from_env() -> tuple[str, ...]:
    return tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip())


STRATEGIES = ("ema", "breakout")


def strategy_name_from_env() -> str:
    name = _first("STRATEGY", default="ema").lower()
    if name not in STRATEGIES:
        raise RuntimeError(f"STRATEGY deve essere uno tra: {', '.join(STRATEGIES)}")
    return name


def breakout_params_from_env() -> BreakoutParams:
    load_dotenv()
    d = BreakoutParams()
    return BreakoutParams(
        entry_period=int(_first("BREAKOUT_ENTRY_DAYS", default=str(d.entry_period))),
        exit_period=int(_first("BREAKOUT_EXIT_DAYS", default=str(d.exit_period))),
        atr_period=int(_first("BREAKOUT_ATR_PERIOD", default=str(d.atr_period))),
        stop_atr=float(_first("BREAKOUT_STOP_ATR", default=str(d.stop_atr))),
    )


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
    # Exchange
    exchange: str  # "bybit" oppure "okx"
    api_key: str
    api_secret: str
    api_passphrase: str  # solo OKX
    testnet: bool  # Bybit testnet / OKX Demo Trading
    region: str  # "global" oppure "eu"
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
    max_position_usdt: float  # nella valuta di quotazione (USDT, USDC, EUR...)
    approval_timeout_sec: int
    max_price_deviation_pct: float
    poll_interval_sec: int
    dry_run: bool
    strategy_name: str = "ema"  # "ema" (incrocio medie) oppure "breakout" (trend following giornaliero)
    breakout: BreakoutParams = BreakoutParams()

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        exchange = exchange_from_env()
        region = region_from_env()
        category = category_from_env()
        symbols = symbols_from_env()
        if exchange == "bybit" and region == "eu" and category != "spot":
            raise RuntimeError(
                "Bybit EU offre solo il mercato spot: imposta CATEGORY=spot "
                "e simboli in USDC o EUR (es. SYMBOLS=BTCUSDC,ETHUSDC)"
            )
        strategy_name = strategy_name_from_env()
        if strategy_name == "breakout" and category != "spot":
            raise RuntimeError("STRATEGY=breakout funziona in spot: imposta CATEGORY=spot")
        prefix = exchange.upper()
        testnet_raw = _first("TESTNET", "BYBIT_TESTNET", default="true")
        return cls(
            exchange=exchange,
            api_key=_required(f"{prefix}_API_KEY"),
            api_secret=_required(f"{prefix}_API_SECRET"),
            api_passphrase=_required("OKX_API_PASSPHRASE") if exchange == "okx" else "",
            testnet=testnet_raw.lower() in {"1", "true", "yes", "on", "si", "sì"},
            region=region,
            category=category,
            symbols=symbols,
            leverage=int(os.getenv("LEVERAGE", "1")),
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=int(_required("TELEGRAM_CHAT_ID")),
            # il breakout lavora sempre su candele giornaliere
            interval="D" if strategy_name == "breakout" else os.getenv("INTERVAL", "15"),
            strategy=strategy_params_from_env(),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "1.0")),
            max_position_usdt=float(os.getenv("MAX_POSITION_USDT", "1000")),
            # breakout giornaliero: il segnale arriva dopo la chiusura UTC (notte in Italia),
            # quindi l'autorizzazione resta valida più a lungo e tollera più movimento
            approval_timeout_sec=int(os.getenv(
                "APPROVAL_TIMEOUT_SEC", "43200" if strategy_name == "breakout" else "300")),
            max_price_deviation_pct=float(os.getenv(
                "MAX_PRICE_DEVIATION_PCT", "3" if strategy_name == "breakout" else "0.5")),
            poll_interval_sec=int(os.getenv("POLL_INTERVAL_SEC", "60")),
            dry_run=_bool("DRY_RUN", True),
            strategy_name=strategy_name,
            breakout=breakout_params_from_env(),
        )

    @property
    def tld(self) -> str:
        return REGION_TLD[self.region]
