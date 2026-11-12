"""ClaTrader: genera segnali su Bybit e chiede l'autorizzazione su Telegram prima di operare."""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from .config import Config
from .exchange import BybitClient
from .risk import position_size
from .strategy import Signal, describe, evaluate

log = logging.getLogger("clatrader")


@dataclass
class PendingTrade:
    id: str
    signal: Signal
    qty: Decimal
    created_at: float
    message_id: int | None = None
    done: bool = False


class ClaTrader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = BybitClient(cfg.bybit_api_key, cfg.bybit_api_secret, cfg.bybit_testnet, cfg.category)
        self.params = cfg.strategy
        self.pending: dict[str, PendingTrade] = {}
        self.last_signal_candle: dict[str, int] = {}
        self.paused = False
        self._exec_lock = asyncio.Lock()

    # ------------------------------------------------------------ helper
    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id == self.cfg.telegram_chat_id

    async def _send(self, app: Application, text: str, **kw):
        return await app.bot.send_message(
            chat_id=self.cfg.telegram_chat_id, text=text, parse_mode=ParseMode.HTML, **kw
        )

    def _mode_label(self) -> str:
        net = "TESTNET" if self.cfg.bybit_testnet else "MAINNET"
        return f"{net}{' · DRY-RUN' if self.cfg.dry_run else ''}"

    # ------------------------------------------------------------ ciclo strategia
    async def scan(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._expire_pending(context.application)
        if self.paused:
            return
        for symbol in self.cfg.symbols:
            try:
                await self._scan_symbol(context.application, symbol)
            except Exception as e:  # noqa: BLE001 - un simbolo non deve bloccare gli altri
                log.exception("Errore durante l'analisi di %s", symbol)
                await self._send(context.application, f"⚠️ Errore su {symbol}: <code>{html.escape(str(e))[:500]}</code>")

    async def _scan_symbol(self, app: Application, symbol: str) -> None:
        candles = await asyncio.to_thread(
            self.client.get_closed_candles, symbol, self.cfg.interval, self.params.min_candles + 50
        )
        if not candles:
            return
        last = candles[-1].start_ms
        if self.last_signal_candle.get(symbol) == last:
            return  # candela già valutata
        self.last_signal_candle[symbol] = last

        signal = evaluate(symbol, candles, self.params)
        if signal is None:
            return
        log.info("Segnale %s %s @ %s", signal.side, symbol, signal.entry)

        if self.cfg.category == "spot" and signal.side == "Sell":
            log.info("Segnale short ignorato su spot")
            return
        if any(p.signal.symbol == symbol and not p.done for p in self.pending.values()):
            return
        if await asyncio.to_thread(self.client.has_open_position, symbol):
            log.info("Posizione già aperta su %s, segnale ignorato", symbol)
            return

        equity = await asyncio.to_thread(self.client.get_equity_usdt)
        rules = await asyncio.to_thread(self.client.get_rules, symbol)
        max_value = self.cfg.max_position_usdt
        if self.cfg.category == "linear":
            max_value = min(max_value, equity * self.cfg.leverage)
        else:
            max_value = min(max_value, equity)
        qty = position_size(
            equity, self.cfg.risk_per_trade_pct, signal.entry, signal.stop_distance, max_value, rules
        )
        if qty <= 0:
            await self._send(
                app,
                f"ℹ️ Segnale {signal.side} su {symbol} scartato: quantità sotto il minimo "
                f"(equity {equity:.2f} USDT).",
            )
            return
        await self._request_approval(app, signal, qty, equity)

    async def _request_approval(self, app: Application, s: Signal, qty: Decimal, equity: float) -> None:
        trade = PendingTrade(id=secrets.token_hex(4), signal=s, qty=qty, created_at=time.time())
        self.pending[trade.id] = trade

        notional = float(qty) * s.entry
        risk = float(qty) * s.stop_distance
        reward = risk * self.params.reward_risk
        candle_t = datetime.fromtimestamp(s.candle_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        emoji = "🟢 LONG" if s.side == "Buy" else "🔴 SHORT"
        ind = s.indicators
        text = (
            f"<b>{emoji} {s.symbol}</b>  <i>[{self._mode_label()}]</i>\n"
            f"{html.escape(s.reason)}\n\n"
            f"Entry: <code>{s.entry:g}</code>\n"
            f"Stop loss: <code>{s.stop_loss:.6g}</code>\n"
            f"Take profit: <code>{s.take_profit:.6g}</code>\n"
            f"Quantità: <code>{qty}</code> (≈ {notional:.2f} USDT)\n"
            f"Rischio: <b>{risk:.2f} USDT</b> ({risk / equity * 100:.2f}% equity) · "
            f"Target: {reward:.2f} USDT\n"
            f"RSI {ind['rsi']:.1f} · ATR {ind['atr']:.6g} · Vol {ind['rel_volume']:.1f}× media · TF {self.cfg.interval} · candela {candle_t}\n\n"
            f"⏳ Scade tra {self.cfg.approval_timeout_sec // 60} min."
        )
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Autorizza", callback_data=f"ok:{trade.id}"),
                InlineKeyboardButton("❌ Rifiuta", callback_data=f"no:{trade.id}"),
            ]]
        )
        msg = await self._send(app, text, reply_markup=kb)
        trade.message_id = msg.message_id

    def _expire_pending(self, app: Application) -> None:
        now = time.time()
        for tid, t in list(self.pending.items()):
            if t.done:
                del self.pending[tid]
            elif now - t.created_at > self.cfg.approval_timeout_sec:
                t.done = True
                del self.pending[tid]
                if t.message_id:
                    app.create_task(
                        app.bot.edit_message_text(
                            chat_id=self.cfg.telegram_chat_id,
                            message_id=t.message_id,
                            text=f"⌛ Segnale {t.signal.side} {t.signal.symbol} scaduto senza autorizzazione.",
                        )
                    )

    # ------------------------------------------------------------ approvazione
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None:
            return
        if not self._authorized(update):
            await q.answer("Non autorizzato", show_alert=True)
            return
        action, _, tid = (q.data or "").partition(":")
        async with self._exec_lock:
            trade = self.pending.get(tid)
            if trade is None or trade.done:
                await q.answer("Segnale non più valido")
                await q.edit_message_reply_markup(reply_markup=None)
                return
            if time.time() - trade.created_at > self.cfg.approval_timeout_sec:
                trade.done = True
                await q.answer("Segnale scaduto")
                await q.edit_message_text(f"⌛ Segnale {trade.signal.symbol} scaduto.")
                return
            trade.done = True
            await q.answer()
            original = q.message.text_html if q.message else ""
            if action != "ok":
                await q.edit_message_text(original + "\n\n❌ <b>Rifiutato</b>", parse_mode=ParseMode.HTML)
                return
            await q.edit_message_text(original + "\n\n⏳ Esecuzione in corso…", parse_mode=ParseMode.HTML)
            result = await self._execute(trade)
            await q.edit_message_text(original + "\n\n" + result, parse_mode=ParseMode.HTML)

    async def _execute(self, trade: PendingTrade) -> str:
        s = trade.signal
        try:
            price = await asyncio.to_thread(self.client.get_last_price, s.symbol)
            deviation = abs(price - s.entry) / s.entry * 100
            if deviation > self.cfg.max_price_deviation_pct:
                return (
                    f"🚫 <b>Annullato</b>: il prezzo si è mosso del {deviation:.2f}% "
                    f"(limite {self.cfg.max_price_deviation_pct}%). Ora {price:g}."
                )
            # Il prezzo potrebbe aver superato SL/TP nel frattempo
            if (s.side == "Buy" and not s.stop_loss < price < s.take_profit) or (
                s.side == "Sell" and not s.take_profit < price < s.stop_loss
            ):
                return f"🚫 <b>Annullato</b>: il prezzo attuale {price:g} è fuori dall'intervallo SL/TP."
            if await asyncio.to_thread(self.client.has_open_position, s.symbol):
                return "🚫 <b>Annullato</b>: esiste già una posizione aperta."
            if self.cfg.dry_run:
                log.info("DRY-RUN: ordine %s %s qty=%s non inviato", s.side, s.symbol, trade.qty)
                return f"🧪 <b>Autorizzato (DRY-RUN)</b>: nessun ordine reale inviato. Prezzo attuale {price:g}."
            await asyncio.to_thread(self.client.ensure_leverage, s.symbol, self.cfg.leverage)
            res = await asyncio.to_thread(self.client.place_market_order, s, trade.qty)
            return f"✅ <b>Ordine inviato</b> · ID <code>{html.escape(str(res.get('orderId')))}</code>"
        except Exception as e:  # noqa: BLE001
            log.exception("Esecuzione fallita")
            return f"⚠️ <b>Errore</b>: <code>{html.escape(str(e))[:500]}</code>"

    # ------------------------------------------------------------ comandi
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_html(
            f"🤖 <b>ClaTrader</b> attivo [{self._mode_label()}]\n"
            f"Simboli: {', '.join(self.cfg.symbols)} · TF {self.cfg.interval} · {self.cfg.category}\n"
            f"Strategia: {html.escape(describe(self.params))}\n"
            f"Rischio per trade: {self.cfg.risk_per_trade_pct}%\n\n"
            "/status /balance /positions /pause /resume /scan"
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        pend = [p for p in self.pending.values() if not p.done]
        await update.effective_message.reply_html(
            f"Stato: {'⏸ in pausa' if self.paused else '▶️ attivo'} [{self._mode_label()}]\n"
            f"Segnali in attesa: {len(pend)}"
        )

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        eq = await asyncio.to_thread(self.client.get_equity_usdt)
        await update.effective_message.reply_html(f"💰 Equity: <b>{eq:.2f} USDT</b>")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        pos = []
        for sym in self.cfg.symbols:
            pos += await asyncio.to_thread(self.client.get_open_positions, sym)
        if not pos:
            await update.effective_message.reply_text("Nessuna posizione aperta.")
            return
        lines = [
            f"{p['symbol']} {p.get('side')} size {p.get('size')} · PnL {p.get('unrealisedPnl')}" for p in pos
        ]
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            self.paused = True
            await update.effective_message.reply_text("⏸ Analisi in pausa.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._authorized(update):
            self.paused = False
            await update.effective_message.reply_text("▶️ Analisi ripresa.")

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.last_signal_candle.clear()
        await update.effective_message.reply_text("🔍 Analisi forzata in corso…")
        await self.scan(context)

    async def on_startup(self, app: Application) -> None:
        for sym in self.cfg.symbols:
            await asyncio.to_thread(self.client.get_rules, sym)
        await self._send(app, f"🤖 ClaTrader avviato [{self._mode_label()}] — /start per i dettagli.")

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Registra l'errore e lo notifica su Telegram, così un comando non resta mai senza risposta."""
        log.error("Errore non gestito", exc_info=context.error)
        try:
            await self._send(
                context.application,
                f"⚠️ Errore: <code>{html.escape(str(context.error))[:500]}</code>",
            )
        except Exception:  # noqa: BLE001 - se anche Telegram fallisce, resta il log
            log.exception("Impossibile notificare l'errore su Telegram")

    # ------------------------------------------------------------ avvio
    def run(self) -> None:
        app = Application.builder().token(self.cfg.telegram_token).post_init(self.on_startup).build()
        only_me = filters.Chat(chat_id=self.cfg.telegram_chat_id)
        for name, fn in {
            "start": self.cmd_start,
            "status": self.cmd_status,
            "balance": self.cmd_balance,
            "positions": self.cmd_positions,
            "pause": self.cmd_pause,
            "resume": self.cmd_resume,
            "scan": self.cmd_scan,
        }.items():
            app.add_handler(CommandHandler(name, fn, filters=only_me))
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^(ok|no):"))
        app.add_error_handler(self.on_error)
        app.job_queue.run_repeating(self.scan, interval=self.cfg.poll_interval_sec, first=5)
        log.info("ClaTrader avviato [%s]", self._mode_label())
        app.run_polling(allowed_updates=Update.ALL_TYPES)
