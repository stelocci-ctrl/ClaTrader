# ClaTrader
Trading con Claude: un bot che analizza il mercato su **OKX** o **Bybit**, genera segnali con una
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

### 2. Chiave API OKX (consigliato in Europa)
OKX Europe (my.okx.com, sede a Malta) ha la **licenza MiCA** e permette il trading via API ai privati,
con un conto **Demo Trading** per le prove.
1. Registrati su **https://my.okx.com** (sito europeo).
2. Passa alla **Demo Trading**: menu *Trade* → *Demo Trading*. Riceverai fondi virtuali.
3. Dentro la Demo Trading: *Personal Center* → **Create Demo Account API key**.
   Permessi **Read** e **Trade** (mai *Withdraw*), scegli una **passphrase** e annotala.
   Le chiavi della Demo funzionano solo in Demo; per il conto reale ne servono altre.
4. Nel `.env`:
   ```
   EXCHANGE=okx
   REGION=eu
   TESTNET=true
   SYMBOLS=BTCUSDC,ETHUSDC
   OKX_API_KEY=...
   OKX_API_SECRET=...
   OKX_API_PASSPHRASE=...
   ```
- Su OKX il bot opera in **spot**, quindi **solo long**, con stop loss e take profit agganciati all'ordine.
- Per il conto reale OKX richiede la verifica dell'identità (KYC).

### 2b. Chiave API Bybit (alternativa)
1. Per i test: crea un account su [testnet.bybit.com](https://testnet.bybit.com) e prendi fondi di prova.
2. *API Management* → *Create New Key* → *System-generated*.
3. Permessi: **Read-Write**, spunta solo *Contracts → Orders/Positions* (e/o *Spot → Trade*).
   **Non abilitare mai Withdraw.** Se il bot gira su un server con IP fisso, vincola la chiave a quell'IP.
4. Serve un conto **Unified Trading Account (UTA)**.

### Bybit EU (conto europeo, bybit.eu)
Bybit EU è un servizio **separato** da bybit.com: chiavi API, server e prodotti diversi.
Se il tuo conto è su **bybit.eu** (testnet: testnet.bybit.eu):
```
REGION=eu
CATEGORY=spot
SYMBOLS=BTCUSDC,ETHUSDC
```
- Bybit EU offre solo lo **spot** (niente perpetual), quindi il bot opera **solo long**.
- Per le regole europee (MiCA) non ci sono coppie in USDT: usa coppie in **USDC** o **EUR**.
  Il saldo e il rischio sono calcolati nella valuta della coppia.
- Con chiavi di bybit.eu ma `REGION=global` Bybit risponde con errore **401**.
- Su alcuni conti Bybit EU il trading via API è bloccato (errore **10024**, restrizioni normative):
  in quel caso usa OKX. `python -m clatrader.diagnose` verifica se il tuo conto può inviare ordini.

### 3. Avvio
```bash
python -m clatrader
```

## Backtest
Usa **gli stessi parametri del bot** letti dal `.env` (simboli, timeframe, categoria e tutti i
parametri della strategia) e i dati storici pubblici dell'exchange scelto: non servono chiavi API.

```bash
python -m clatrader.backtest                        # tutto dal .env
python -m clatrader.backtest BTCUSDT ETHUSDT -i 60 -n 5000   # simboli, timeframe e n. candele
VOLUME_MULT=0 python -m clatrader.backtest          # confronto senza filtro volume
python -m clatrader.backtest -c spot                # solo long, fee spot
```

### Trend following giornaliero (breakout)
```bash
python -m clatrader.backtest -s breakout                       # candele giornaliere, ~2000 giorni
python -m clatrader.backtest -s breakout -r global BTCUSDT ETHUSDT   # storico più lungo (coppie USDT)
```
Entra quando la chiusura supera il massimo degli ultimi 20 giorni (all'apertura del giorno dopo),
stop iniziale a 2×ATR, esce quando la chiusura scende sotto il minimo degli ultimi 10 giorni.
Nel bot si attiva con `STRATEGY=breakout`: ogni giorno, dopo la chiusura della candela giornaliera
(00:00 UTC, cioè l'1 o le 2 di notte in Italia), se c'è un breakout ricevi la richiesta di autorizzazione
(valida 12 ore). L'ordine parte con il solo **stop loss** agganciato; l'**uscita è automatica** quando la
chiusura scende sotto il minimo di `BREAKOUT_EXIT_DAYS` giorni (il bot cancella lo stop e vende ciò che
aveva comprato). La quantità viene ricalcolata al momento dell'esecuzione sul prezzo attuale.

Il backtest mostra risultato prima/dopo i costi (commissioni + slippage), drawdown, confronto con buy & hold
e una **prova di robustezza** su 12 combinazioni di parametri: una strategia credibile è positiva
nella gran parte delle combinazioni e su più monete, non in una sola.

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
| `/close SIMBOLO` o `/close all` | chiude subito le posizioni spot aperte dal bot (cancella lo stop e vende) |
| `/scan` | mostra prezzo, medie, RSI e volume di ogni simbolo e **quale condizione manca** per un segnale, poi analizza |
| `/test [SIMBOLO]` | **solo testnet**: crea un segnale LONG di prova al prezzo attuale per provare autorizzazione e ordini |

## Percorso consigliato verso il conto reale
1. `TESTNET=true`, `DRY_RUN=true` → verifica che i messaggi arrivino.
2. `TESTNET=true`, `DRY_RUN=false` → ordini veri sul testnet.
3. Backtest (vedi sopra). Valuta almeno 30+ trade.
4. Solo allora `TESTNET=false` con rischio basso (es. `RISK_PER_TRADE_PCT=0.5`).

## Struttura
```
clatrader/
  config.py      configurazione da .env
  indicators.py  EMA, RSI, ATR in puro Python
  strategy.py    regole di ingresso, SL/TP
  risk.py        dimensionamento posizione e arrotondamenti
  exchange.py    client Bybit v5 (pybit) e scelta dell'exchange
  okx.py         client OKX spot (ccxt)
  diagnose.py    diagnosi ordini su testnet Bybit
  bot.py         bot Telegram, approvazioni ed esecuzione
  backtest.py    backtest su dati storici Bybit/OKX
tests/           test unitari (pytest)
```

I test girano automaticamente su GitHub Actions (Python 3.11–3.13) a ogni pull request e
push su `main`. In locale: `pip install -r requirements-dev.txt && python -m pytest`.

## Note
- **Spot**: il bot tiene traccia solo delle posizioni che apre lui, nel file `clatrader_positions.json`
  (cartella del progetto). Le monete che possiedi già non bloccano i segnali. Quando stop loss o
  take profit vendono, ricevi un avviso 🔔 su Telegram. Non cancellare quel file con posizioni aperte.
- I segnali in attesa sono in memoria: se riavvii il bot, quelli non autorizzati si perdono
  (le posizioni aperte restano sull'exchange con i loro SL/TP).
- Tieni il bot sempre acceso (VPS, Raspberry Pi, `systemd`, `tmux`…) per ricevere i segnali.
