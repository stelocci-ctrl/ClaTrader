# ClaTrader
Trading con Claude: un bot che analizza il mercato su **Bybit**, genera segnali con una
strategia trend-following e **ti chiede l'autorizzazione su Telegram** prima di aprire
qualsiasi operazione. Nessun ordine parte senza il tuo ✅.

> ⚠️ Il trading con leva comporta un rischio elevato di perdita. Questo software è fornito
> a scopo educativo, senza garanzie: nessuna strategia garantisce profitti. Inizia in
> **testnet** e con **DRY_RUN=true**.

## Come funziona

1. Ogni `POLL_INTERVAL_SEC` il bot scarica le candele chiuse di ogni simbolo.
2. Se la strategia trova un segnale, calcola la quantità in base al rischio e ti manda un
   messaggio Telegram con entry, stop loss, take profit, quantità e rischio in USDT,
   con i pulsanti **✅ Autorizza** / **❌ Rifiuta**.
3. Se autorizzi entro `APPROVAL_TIMEOUT_SEC`, il bot ricontrolla il prezzo (annulla se si è
   mosso più di `MAX_PRICE_DEVIATION_PCT`%, o se c'è già una posizione aperta) e invia un
   ordine a mercato con **SL e TP già agganciati** sull'exchange.
4. Il messaggio viene aggiornato con l'esito (ID ordine o errore).

Solo la chat indicata in `TELEGRAM_CHAT_ID` può dare comandi o autorizzare.

## Strategia (EMA crossover + filtro trend + RSI + volume + ATR)

| | Long | Short |
|---|---|---|
| Trigger | EMA 9 incrocia **sopra** EMA 21 | EMA 9 incrocia **sotto** EMA 21 |
| Filtro trend | prezzo > EMA 200 | prezzo < EMA 200 |
| Filtro RSI(14) | RSI < 70 | RSI > 30 |
| Filtro volume | volume ≥ 1,5 × media 20 candele | volume ≥ 1,5 × media 20 candele |
| Stop loss | entry − 1,5 × ATR(14) | entry + 1,5 × ATR(14) |
| Take profit | entry + 2 × distanza stop | entry − 2 × distanza stop |

Il **filtro volumetrico** scarta gli incroci avvenuti con poca partecipazione del mercato
(spesso falsi segnali in fasi laterali): la candela del segnale deve avere un volume almeno
`VOLUME_MULT` volte la media delle `VOLUME_PERIOD` candele precedenti. `VOLUME_MULT=0` lo disattiva.
Il volume relativo è riportato nel messaggio Telegram.

**Dimensionamento**: la quantità è tale che, se scatta lo stop, perdi `RISK_PER_TRADE_PCT`%
dell'equity (default 1%), con un tetto di `MAX_POSITION_USDT` e di equity × leva.
In modalità `spot` i segnali short vengono ignorati. Tutti i parametri sono nel `.env`.

## Installazione

```bash
git clone https://github.com/stelocci-ctrl/ClaTrader.git && cd ClaTrader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # poi compila le chiavi
```

### 1. Bot Telegram
1. Apri [@BotFather](https://t.me/BotFather) → `/newbot` → copia il token in `TELEGRAM_BOT_TOKEN`.
2. Scrivi a [@userinfobot](https://t.me/userinfobot) per ottenere il tuo id → `TELEGRAM_CHAT_ID`.
3. Apri la chat con il tuo nuovo bot e premi **Start** (altrimenti non può scriverti).

### 2. Chiave API Bybit
1. Per i test: crea un account su [testnet.bybit.com](https://testnet.bybit.com) e prendi fondi di prova.
2. *API Management* → *Create New Key* → *System-generated*.
3. Permessi: **Read-Write**, spunta solo *Contracts → Orders/Positions* (e/o *Spot → Trade*).
   **Non abilitare mai Withdraw.** Se il bot gira su un server con IP fisso, vincola la chiave a quell'IP.
4. Serve un conto **Unified Trading Account (UTA)**.

### 3. Avvio
```bash
python -m clatrader
```

## Backtest
Usa **gli stessi parametri del bot** letti dal `.env` (simboli, timeframe, categoria e tutti i
parametri della strategia) e i dati storici pubblici di Bybit: non servono chiavi API.

```bash
python -m clatrader.backtest                        # tutto dal .env
python -m clatrader.backtest BTCUSDT ETHUSDT -i 60 -n 5000   # simboli, timeframe e n. candele
VOLUME_MULT=0 python -m clatrader.backtest          # confronto senza filtro volume
python -m clatrader.backtest -c spot                # solo long, fee spot
```

I risultati sono in **R** (1 R = la perdita di uno stop), commissioni taker incluse.
Il backtest è volutamente semplice: ingresso alla chiusura della candela, nessuno slippage,
e se nella stessa candela vengono toccati sia stop sia target conta lo stop.

## Comandi Telegram
| Comando | Descrizione |
|---|---|
| `/start` | riepilogo configurazione |
| `/status` | stato e segnali in attesa |
| `/balance` | equity in USDT |
| `/positions` | posizioni aperte sui simboli monitorati |
| `/pause` / `/resume` | sospende / riprende l'analisi |
| `/scan` | forza subito un'analisi |

## Percorso consigliato verso il conto reale
1. `BYBIT_TESTNET=true`, `DRY_RUN=true` → verifica che i messaggi arrivino.
2. `BYBIT_TESTNET=true`, `DRY_RUN=false` → ordini veri sul testnet.
3. Backtest (vedi sotto). Valuta almeno 30+ trade.
4. Solo allora `BYBIT_TESTNET=false` con rischio basso (es. `RISK_PER_TRADE_PCT=0.5`).

## Struttura
```
clatrader/
  config.py      configurazione da .env
  indicators.py  EMA, RSI, ATR in puro Python
  strategy.py    regole di ingresso, SL/TP
  risk.py        dimensionamento posizione e arrotondamenti
  exchange.py    client Bybit v5 (pybit)
  bot.py         bot Telegram, approvazioni ed esecuzione
  backtest.py    backtest su dati storici Bybit
tests/           test unitari (pytest)
```

## Note
- I segnali in attesa sono in memoria: se riavvii il bot, quelli non autorizzati si perdono
  (le posizioni aperte restano su Bybit con i loro SL/TP).
- Tieni il bot sempre acceso (VPS, Raspberry Pi, `systemd`, `tmux`…) per ricevere i segnali.
