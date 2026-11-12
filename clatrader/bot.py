"""ClaTrader: genera segnali su Bybit e chiede l'autorizzazione su Telegram prima di operare."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

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
from .exchange import make_client
from .breakout import BreakoutParams
from .breakout import analyze as analyze_breakout
from .positions import SpotPositionBook
from .risk import position_size, round_down_to_step
from .strategy import Analysis, Signal, analyze, describe

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
        self.client = make_client(cfg)
        self.book = SpotPositionBook(Path(os.getenv("STATE_FILE", "clatrader_positions.json")))
        self.params = cfg.strategy
        self.strategy_name = getattr(cfg, "strategy_name", "ema")
        self.breakout = getattr(cfg, "breakout", BreakoutParams())
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

    @property
    def is_breakout(self) -> bool:
        return getattr(self, "strategy_name", "ema") == "breakout"

    def _describe_strategy(self) -> str:
        if self.is_breakout:
            b = self.breakout
            return (
                f"Trend following breakout giornaliero: entrata sopra il massimo a {b.entry_period} giorni, "
                f"stop {b.stop_atr:g}×ATR{b.atr_period}, uscita automatica sotto il minimo a {b.exit_period} giorni"
            )
        return describe(self.params)

    def _mode_label(self) -> str:
        exchange = getattr(self.cfg, "exchange", "bybit")
        if exchange == "okx":
            name = "OKX Europe" if self.cfg.region == "eu" else "OKX"
            net = f"{name} {'DEMO' if self.cfg.testnet else 'CONTO REALE'}"
        else:
            net = "TESTNET" if self.cfg.testnet else "MAINNET"
            if self.cfg.region == "eu":
                net = f"Bybit EU {net}"
        return f"{net}{' · DRY-RUN' if self.cfg.dry_run else ''}"

    # ------------------------------------------------------------ ciclo strategia
    # ------------------------------------------------------------ posizioni
    async def _has_position(self, symbol: str) -> bool:
        """Spot: solo le posizioni aperte dal bot (non le monete che possiedi già)."""
        if self.cfg.category != "spot":
            return await asyncio.to_thread(self.client.has_open_position, symbol)
        rec = self.book.get(symbol)
        if rec is None:
            return False
        base = await asyncio.to_thread(self.client.base_currency, symbol)
        balance = await asyncio.to_thread(self.client.get_coin_balance, base)
        return self.book.is_still_open(rec, balance)

    async def _check_closed_positions(self, app: Application) -> None:
        """Avvisa quando una posizione spot del bot si chiude (stop, target o vendita manuale)."""
        for symbol in self.book.symbols():
            try:
                if await self._has_position(symbol):
                    continue
                price = await asyncio.to_thread(self.client.get_last_price, symbol)
            except Exception:  # noqa: BLE001 - riproveremo al prossimo giro
                log.exception("Controllo posizione %s fallito", symbol)
                continue
            rec = self.book.close(symbol)
            if rec is None:
                continue
            change = (price - rec["entry"]) / rec["entry"] * 100
            if price <= rec["stop_loss"] * 1.002:
                how = "🔴 probabilmente è scattato lo <b>stop loss</b>"
            elif rec.get("take_profit") and price >= rec["take_profit"] * 0.998:
                how = "🟢 probabilmente è stato raggiunto il <b>take profit</b>"
            else:
                how = "chiusa (stop, target o vendita manuale)"
            await self._send(
                app,
                f"🔔 Posizione <b>{symbol}</b> chiusa: {how}.\n"
                f"Ingresso {rec['entry']:g} · prezzo attuale {price:g} ({change:+.2f}%)",
            )

    async def scan(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._expire_pending(context.application)
        await self._check_closed_positions(context.application)
        if self.is_breakout:
            await self._check_exits(context.application)  # le uscite valgono anche in pausa
        if self.paused:
            return
        for symbol in self.cfg.symbols:
            try:
                await self._scan_symbol(context.application, symbol)
            except Exception as e:  # noqa: BLE001 - un simbolo non deve bloccare gli altri
                log.exception("Errore durante l'analisi di %s", symbol)
                await self._send(context.application, f"⚠️ Errore su {symbol}: <code>{html.escape(str(e))[:500]}</code>")

    async def _candles(self, symbol: str):
        need = self.breakout.warmup + 30 if self.is_breakout else self.params.min_candles + 50
        return await asyncio.to_thread(self.client.get_closed_candles, symbol, self.cfg.interval, need)

    async def _analyze(self, symbol: str):
        """Scarica le candele chiuse e analizza l'ultima. Ritorna anche il suo timestamp."""
        candles = await self._candles(symbol)
        if self.is_breakout:
            if not candles:
                return analyze_breakout(symbol, [], self.breakout), None
            return analyze_breakout(symbol, candles, self.breakout), candles[-1].start_ms
        if not candles:
            return Analysis(), None
        return analyze(symbol, candles, self.params), candles[-1].start_ms

    async def _check_exits(self, app: Application) -> None:
        """Breakout: uscita AUTOMATICA quando la chiusura scende sotto il minimo di uscita."""
        for symbol in self.book.symbols():
            rec = self.book.get(symbol)
            try:
                if rec is None or not await self._has_position(symbol):
                    continue
                a, last = await self._analyze(symbol)
                if last is None or not a.exit or rec.get("exit_attempt_ms") == last:
                    continue
                self.book.update(symbol, exit_attempt_ms=last)  # un solo tentativo per candela
                await self._auto_exit(app, symbol, rec, a)
            except Exception as e:  # noqa: BLE001
                log.exception("Uscita automatica %s fallita", symbol)
                await self._send(
                    app,
                    f"⚠️ <b>Uscita automatica {symbol} NON riuscita</b>: <code>{html.escape(str(e))[:300]}</code>\n"
                    "Controlla la posizione su exchange e, se serve, vendi a mano.",
                )

    async def _auto_exit(self, app: Application, symbol: str, rec: dict, a) -> None:
        base = await asyncio.to_thread(self.client.base_currency, symbol)
        balance = await asyncio.to_thread(self.client.get_coin_balance, base)
        rules = await asyncio.to_thread(self.client.get_rules, symbol)
        # vendiamo solo ciò che ha comprato il bot (al netto della commissione presa in moneta base)
        qty = round_down_to_step(min(rec["qty"], balance - rec["base_before"]), rules.qty_step)
        if self.cfg.dry_run:
            await self._send(app, f"🧪 DRY-RUN: segnale di uscita su {symbol}, nessun ordine inviato.")
            return
        warn = ""
        try:
            await asyncio.to_thread(self.client.cancel_protective_orders, symbol)
        except Exception as e:  # noqa: BLE001
            log.exception("Cancellazione stop %s fallita", symbol)
            warn = (
                f"\n⚠️ Non sono riuscito a cancellare lo stop loss ({html.escape(str(e))[:150]}): "
                "cancellalo a mano su exchange."
            )
        if qty >= rules.min_qty:
            await asyncio.to_thread(self.client.sell_market, symbol, qty)
        price = await asyncio.to_thread(self.client.get_last_price, symbol)
        self.book.close(symbol)
        risk = rec["entry"] - rec["stop_loss"]
        r = (price - rec["entry"]) / risk if risk > 0 else 0.0
        change = (price - rec["entry"]) / rec["entry"] * 100
        await self._send(
            app,
            f"🏁 <b>Uscita automatica {symbol}</b>: chiusura {a.price:g} sotto il minimo a "
            f"{self.breakout.exit_period} giorni ({a.channel_low:g}).\n"
            f"Venduti {qty} a circa {price:g} · ingresso {rec['entry']:g} · "
            f"risultato <b>{r:+.2f} R</b> ({change:+.2f}%){warn}",
        )

    async def _scan_symbol(self, app: Application, symbol: str) -> None:
        analysis, last = await self._analyze(symbol)
        if last is None or self.last_signal_candle.get(symbol) == last:
            return  # nessun dato o candela già valutata
        self.last_signal_candle[symbol] = last
        if analysis.signal is None:
            return
        log.info("Segnale %s %s @ %s", analysis.signal.side, symbol, analysis.signal.entry)
        await self._propose(app, analysis.signal)

    async def _propose(self, app: Application, signal: Signal) -> None:
        """Controlli, dimensionamento e richiesta di autorizzazione su Telegram."""
        symbol = signal.symbol
        if self.cfg.category == "spot" and signal.side == "Sell":
            log.info("Segnale short ignorato su spot")
            return
        if any(p.signal.symbol == symbol and not p.done for p in self.pending.values()):
            await self._send(app, f"ℹ️ Segnale su {symbol} ignorato: ce n'è già uno in attesa di risposta.")
            return
        if await self._has_position(symbol):
            log.info("Posizione già aperta su %s, segnale ignorato", symbol)
            await self._send(app, f"ℹ️ Segnale su {symbol} ignorato: posizione già aperta.")
            return

        ccy = await asyncio.to_thread(self.client.quote_currency, symbol)
        qty, equity = await self._size(symbol, signal.entry, signal.stop_distance)
        if qty <= 0:
            await self._send(
                app,
                f"ℹ️ Segnale {signal.side} su {symbol} scartato: quantità sotto il minimo "
                f"(saldo {equity:.2f} {ccy}).",
            )
            return
        await self._request_approval(app, signal, qty, equity, ccy)

    async def _size(self, symbol: str, entry: float, stop_distance: float) -> tuple[Decimal, float]:
        """Quantità che rischia RISK_PER_TRADE_PCT del saldo, con i tetti di posizione."""
        equity = await asyncio.to_thread(self.client.get_equity, symbol)
        rules = await asyncio.to_thread(self.client.get_rules, symbol)
        max_value = self.cfg.max_position_usdt
        if self.cfg.category == "linear":
            max_value = min(max_value, equity * self.cfg.leverage)
        else:
            max_value = min(max_value, equity)  # spot: mai più del saldo disponibile
        qty = position_size(equity, self.cfg.risk_per_trade_pct, entry, stop_distance, max_value, rules)
        return qty, equity

    async def _request_approval(
        self, app: Application, s: Signal, qty: Decimal, equity: float, ccy: str
    ) -> None:
        trade = PendingTrade(id=secrets.token_hex(4), signal=s, qty=qty, created_at=time.time())
        self.pending[trade.id] = trade

        notional = float(qty) * s.entry
        risk = float(qty) * s.stop_distance
        candle_t = datetime.fromtimestamp(s.candle_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        emoji = "🟢 LONG" if s.side == "Buy" else "🔴 SHORT"
        ind = s.indicators
        text = (
            f"<b>{emoji} {s.symbol}</b>  <i>[{self._mode_label()}]</i>\n"
            f"{html.escape(s.reason)}\n\n"
            f"Entry: <code>{s.entry:g}</code>\n"
            f"Stop loss: <code>{s.stop_loss:.6g}</code>\n"
            + (
                f"Take profit: <code>{s.take_profit:.6g}</code>\n"
                if s.take_profit is not None
                else f"Take profit: nessuno · uscita automatica sotto il minimo a {self.breakout.exit_period} giorni\n"
            )
            + f"Quantità: <code>{qty}</code> (≈ {notional:.2f} {ccy}, {notional / equity * 100:.0f}% del saldo)\n"
            f"Rischio: <b>{risk:.2f} {ccy}</b> ({risk / equity * 100:.2f}% del saldo)"
            + (
                f" · Target: {risk * self.params.reward_risk:.2f} {ccy}\n"
                if s.take_profit is not None
                else "\n"
            )
            + " · ".join(
                part for part in (
                    f"RSI {ind['rsi']:.1f}" if "rsi" in ind else "",
                    f"ATR {ind['atr']:.6g}" if "atr" in ind else "",
                    f"Vol {ind['rel_volume']:.1f}× media" if "rel_volume" in ind else "",
                    f"TF {self.cfg.interval}",
                    f"candela {candle_t}",
                ) if part
            )
            + "\n\n"
            + (
                f"⏳ Scade tra {self.cfg.approval_timeout_sec / 3600:g} ore."
                if self.cfg.approval_timeout_sec >= 3600
                else f"⏳ Scade tra {self.cfg.approval_timeout_sec // 60} min."
            )
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
            tp_hi = s.take_profit if s.take_profit is not None else float("inf")
            tp_lo = s.take_profit if s.take_profit is not None else float("-inf")
            if (s.side == "Buy" and not s.stop_loss < price < tp_hi) or (
                s.side == "Sell" and not tp_lo < price < s.stop_loss
            ):
                return f"🚫 <b>Annullato</b>: il prezzo attuale {price:g} è fuori dall'intervallo SL/TP."
            if await self._has_position(s.symbol):
                return "🚫 <b>Annullato</b>: esiste già una posizione aperta."
            # ricalcola la quantità sul prezzo attuale: lo stop è fisso, quindi il rischio resta quello scelto
            qty, _ = await self._size(s.symbol, price, abs(price - s.stop_loss))
            if qty <= 0:
                return "🚫 <b>Annullato</b>: quantità sotto il minimo dell'exchange."
            if qty != trade.qty:
                log.info("Quantità ricalcolata %s -> %s (prezzo %s)", trade.qty, qty, price)
                trade.qty = qty
            if self.cfg.dry_run:
                log.info("DRY-RUN: ordine %s %s qty=%s non inviato", s.side, s.symbol, trade.qty)
                return f"🧪 <b>Autorizzato (DRY-RUN)</b>: nessun ordine reale inviato. Prezzo attuale {price:g}."
            await asyncio.to_thread(self.client.ensure_leverage, s.symbol, self.cfg.leverage)
            base_before = 0.0
            if self.cfg.category == "spot":
                base = await asyncio.to_thread(self.client.base_currency, s.symbol)
                base_before = await asyncio.to_thread(self.client.get_coin_balance, base)
            res = await asyncio.to_thread(self.client.place_market_order, s, trade.qty)
            if self.cfg.category == "spot":
                self.book.open(s.symbol, float(trade.qty), base_before, s.entry, s.stop_loss, s.take_profit)
            return (
                f"✅ <b>Ordine inviato</b> · quantità {trade.qty} · "
                f"ID <code>{html.escape(str(res.get('orderId')))}</code>"
            )
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
            f"Strategia: {html.escape(self._describe_strategy())}\n"
            f"Rischio per trade: {self.cfg.risk_per_trade_pct}%\n\n"
            "/status /balance /positions /pause /resume /scan /test"
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
        lines, seen = [], set()
        for sym in self.cfg.symbols:
            ccy = await asyncio.to_thread(self.client.quote_currency, sym)
            if ccy in seen:
                continue
            seen.add(ccy)
            eq = await asyncio.to_thread(self.client.get_equity, sym)
            lines.append(f"💰 Saldo: <b>{eq:.2f} {ccy}</b>")
        await update.effective_message.reply_html("\n".join(lines))

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.cfg.category == "spot":
            lines = []
            for sym in self.cfg.symbols:
                rec = self.book.get(sym)
                if rec is None or not await self._has_position(sym):
                    continue
                price = await asyncio.to_thread(self.client.get_last_price, sym)
                change = (price - rec["entry"]) / rec["entry"] * 100
                lines.append(
                    f"{sym} LONG {rec['qty']:g} @ {rec['entry']:g} · SL {rec['stop_loss']:.6g} · "
                    + (f"TP {rec['take_profit']:.6g}" if rec.get("take_profit") else "uscita automatica")
                    + f" · ora {price:g} ({change:+.2f}%)"
                )
            await update.effective_message.reply_text(
                "\n".join(lines) if lines else "Nessuna posizione aperta dal bot."
            )
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
        """Mostra lo stato degli indicatori e cosa manca per un segnale, poi analizza."""
        if not self._authorized(update):
            return
        p = self.params
        lines = [f"🔍 <b>Analisi</b> · TF {html.escape(self.cfg.interval)}"]
        for sym in self.cfg.symbols:
            try:
                a, _ = await self._analyze(sym)
            except Exception as e:  # noqa: BLE001
                lines.append(f"\n<b>{sym}</b>: ⚠️ <code>{html.escape(str(e))[:200]}</code>")
                continue
            if self.is_breakout:
                b = self.breakout
                if not a.ready:
                    lines.append(f"\n<b>{sym}</b>: dati insufficienti (servono {b.warmup + 1} giorni)")
                    continue
                gap = (a.channel_high / a.price - 1) * 100
                lines.append(
                    f"\n<b>{sym}</b> chiusura <code>{a.price:g}</code>\n"
                    f"massimo {b.entry_period}g {a.channel_high:g} · minimo {b.exit_period}g {a.channel_low:g} · "
                    f"ATR {a.atr:.6g}"
                )
                if a.signal:
                    lines.append("✅ Breakout: segnale LONG!")
                else:
                    lines.append(f"Nessun breakout: la chiusura è {gap:.1f}% sotto il massimo a {b.entry_period} giorni")
                if self.book.get(sym) and a.exit:
                    lines.append("🏁 Condizione di uscita attiva per la posizione aperta")
                continue
            if not a.ready:
                lines.append(f"\n<b>{sym}</b>: dati insufficienti (servono {p.min_candles} candele)")
                continue
            fast_vs = "sopra" if a.ema_fast > a.ema_slow else "sotto"
            lines.append(
                f"\n<b>{sym}</b> prezzo <code>{a.price:g}</code>\n"
                f"EMA{p.ema_fast} {a.ema_fast:.6g} {fast_vs} EMA{p.ema_slow} {a.ema_slow:.6g} · "
                f"EMA{p.ema_trend} {a.ema_trend:.6g}\n"
                f"RSI {a.rsi:.1f} · ATR {a.atr:.6g} · volume {a.rel_volume:.1f}× media"
            )
            if a.signal:
                lines.append(f"✅ Segnale {'LONG' if a.signal.side == 'Buy' else 'SHORT'}!")
                continue
            sides = [("LONG", a.missing_long)]
            if self.cfg.category != "spot":
                sides.append(("SHORT", a.missing_short))
            for name, missing in sides:
                lines.append(f"{name}: manca " + html.escape("; ".join(missing)))
        await update.effective_message.reply_html("\n".join(lines))
        self.last_signal_candle.clear()
        await self.scan(context)

    async def cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Solo testnet: crea un segnale LONG di prova al prezzo attuale per provare il flusso."""
        if not self._authorized(update):
            return
        if not self.cfg.testnet:
            await update.effective_message.reply_text("🚫 /test funziona solo con TESTNET=true (testnet o conto demo).")
            return
        sym = (context.args[0].upper() if context.args else self.cfg.symbols[0])
        if sym not in self.cfg.symbols:
            await update.effective_message.reply_text(f"Simbolo non configurato. Usa: {', '.join(self.cfg.symbols)}")
            return
        a, last = await self._analyze(sym)
        price = await asyncio.to_thread(self.client.get_last_price, sym)
        # sul testnet il prezzo può essere quasi fermo: almeno 1% di distanza dello stop
        if self.is_breakout:
            stop = max(a.atr * self.breakout.stop_atr, price * 0.01)
            tp = None
            ind = {"atr": a.atr}
        else:
            stop = max(a.atr * self.params.atr_sl_mult, price * 0.01)
            tp = price + stop * self.params.reward_risk
            ind = {"rsi": a.rsi, "atr": a.atr, "rel_volume": a.rel_volume}
        signal = Signal(
            symbol=sym,
            side="Buy",
            entry=price,
            stop_loss=price - stop,
            take_profit=tp,
            candle_start_ms=last or int(time.time() * 1000),
            reason="🧪 SEGNALE DI PROVA creato con /test (non è un segnale della strategia)",
            indicators=ind,
        )
        await self._propose(context.application, signal)

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
            "test": self.cmd_test,
        }.items():
            app.add_handler(CommandHandler(name, fn, filters=only_me))
        app.add_handler(CallbackQueryHandler(self.on_callback, pattern=r"^(ok|no):"))
        app.add_error_handler(self.on_error)
        app.job_queue.run_repeating(self.scan, interval=self.cfg.poll_interval_sec, first=5)
        log.info("ClaTrader avviato [%s]", self._mode_label())
        app.run_polling(allowed_updates=Update.ALL_TYPES)
