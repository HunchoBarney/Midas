# cbcl-nautilus

NautilusTrader-oriented platform for the Coinbase-vs-Chainlink divergence strategy on Polymarket, starting with `cb_cl_005`.

## What is built

- Runtime config for live, realistic paper, dashboard, replay, and backtest commands
- Shared domain models for markets, books, intents, fills, and dashboard snapshots
- A `CbClDivergenceStrategy` with the current `cb_cl_005` rules:
  - `0.05%` threshold
  - `2.0` minute entry window for both `5m` and `15m`
  - `0.60` min buy, `0.90` hard max buy, `0.02` drift cap
- A separate Kelly sizing engine that mirrors the `loguetown` formula and bootstrap hysteresis
- A shared execution core for adaptive book walking and cap/drift enforcement
- A realistic Polymarket-aware paper executor with modeled execution delays
- A real dashboard shell with:
  - structured JSON endpoints
  - overview, opportunities, portfolio, execution, system, and settings surfaces
  - a premium operator UI served from local static assets
  - honest empty states for sections that still require live market/runtime wiring

## Runtime baseline

NautilusTrader currently targets Python `3.12` to `3.14`. This repository is pinned to `3.12` for the initial build because the local system Python here is `3.9.6`, which is too old for Nautilus.

## Quickstart

```bash
uv python install 3.12
uv sync --dev
uv run pytest
```

Operator commands:

```bash
uv run start-live
uv run start-paper
uv run start-dashboard --duration-seconds 10
uv run run-replay
uv run run-backtest
```

`start-paper` always means the realistic paper model. It uses live feed timing as observed, then simulates only the execution path:

- internal processing delay
- signing delay
- submit RTT
- ack/user visibility delay
- confirmation delay
- live book evaluation at simulated submit time

`start-dashboard` now serves:

- `/` for the operator UI
- `/api/bootstrap` for the full dashboard payload
- `/api/overview`
- `/api/opportunities`
- `/api/portfolio`
- `/api/execution`
- `/api/system`
- `/api/settings`
- `/snapshot.json`
- `/healthz`

## Next build steps

- Wire actual Nautilus Polymarket connectivity and rolling market discovery
- Add the real Coinbase spot and Polymarket RTDS clients
- Hook the live execution adapter to the official Nautilus Polymarket execution path
- Wire live runtime state into the dashboard opportunity, portfolio, and execution sections
- Add replay/catalog ingestion and resolution workflows

More detail is in [docs/architecture.md](/Users/karanvirkang/Documents/code/TESTTTTT/docs/architecture.md).
