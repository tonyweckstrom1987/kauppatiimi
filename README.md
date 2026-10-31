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

## Tests

```
python3 -m unittest discover -s . -p "test*.py"
```

Standard library only, Python 3.13. Code identifiers and comments are mostly Finnish (ASCII).
