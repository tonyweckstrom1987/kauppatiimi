# kauppatiimi

A paper-trading research project: a small "team" of LLM agents (analyst + critic) that trades **simulated money only**
on daily crypto spot data, wrapped in rules enforced by code rather than by prompts.

This is a learning and portfolio project. **Nothing here is financial advice and no real money is traded.**

## What is in here

- **Risk guard in code** (`rajat.py`, `riskivahti.py`, `salkku.py`): position limits, cash buffer, daily loss stop,
  drawdown emergency brake with a manual reset, and a kill switch file. The models cannot change these.
- **Honest backtesting** (`backtest.py`, `k7.py`, `laaja.py`): walk-forward evaluation with a locked holdout period,
  a random-strategy baseline, multiple-comparison correction with a fixed hypothesis budget, and a buy-and-hold BTC benchmark.
  `laaja.py` builds a point-in-time universe that includes coins that were later delisted, to measure survivorship and selection bias.
- **Data** (`lataa.py`, `historia_lataus.py`, `lahde.py`): daily candles and funding rates from the public Binance data archive.
- **Agents** (`agentit.py`, `paiva.py`, `k7.py`): LLM calls are strictly JSON-in/JSON-out, data is passed as untrusted blocks, and
  every run is capped in calls and cost.
- **Ops** (`sykemittari.py`, `retro.py`, systemd units): health checks, weekly retrospective, Telegram notifications.
- **Verokirjanpito** (`raportti.py`): paivaraportin ja retron rivi "Realisoitunut tulos (paperi, FIFO)" + `python3 -m raportti vero 2026 [flash|pro]`. Kutsuu vain `kirjanpito.fifo` ja `vuosiyhteenveto`.

## Findings so far (October 2026)

- A simple momentum strategy looked good on a hand-picked, hindsight coin list (+68 % to +159 % out of sample),
  but lost 42 % to 72 % once the universe was rebuilt point-in-time. The selection bias was far larger than the survivorship bias.
- No tested strategy has beaten buy-and-hold BTC on both return and Sharpe ratio.

## A/B statistics

`python3 ab_tilasto.py` compares the flash and pro portfolios (read-only): daily-return difference, Sharpe, max drawdown,
paired block-bootstrap CI and the number of days needed for significance. Tests: `test_ab_tilasto.py` (synthetic data).

## Passive paper portfolio and comparison

`passiivinen_kk.py` runs the paper portfolio `passiivinen_kk` (BTC, 12 equal monthly buys of start capital / 12, never sells, real fees and slippage,
Telegram alert at -30 % from peak; alert only, nothing is changed). `passiivinen_vertailu.py` compares BTC, S&P 500 (price index, read by `indeksidata.py` from a CSV file that the owner downloads manually; the code fetches nothing from the network) and
80/20 and 50/50 mixes from 12 different start months, read-only; the holdout stays locked. Tests: `test_passiivinen_kk.py`.

## Daily portfolio report

`python3 salkkuraportti.py --salkku-db <salkku.db> [--viikko] [--ei-tg]` prints (and sends to Telegram) a read-only report of
at most 6 lines: value and change (EUR and %), trades and costs, max drawdown so far, the same starting capital as a passive
monthly purchase (`mittarit.passiiviset`), and one sentence on what the agent did. `--viikko` adds the weekly summary
(who is ahead, worst day, how often the daily trade limit was hit). Tests: `test_salkkuraportti.py`.

## Tests

```
python3 -m unittest discover -s . -p "test*.py"
```

Standard library only, Python 3.13. Code identifiers and comments are mostly Finnish (ASCII).
