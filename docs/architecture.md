# Architecture

## Current implementation

The repo now has the core strategy/execution slice implemented in local code:

- `cb_cl_005` strategy rules
- Kelly sizing with bootstrap hysteresis
- shared execution core
- realistic Polymarket-aware paper execution
- operator commands
- a real dashboard state layer plus operator UI shell
- structured dashboard JSON endpoints
- premium frontend shell for overview, opportunities, portfolio, execution, system, and settings

## Strategy invariants

- Threshold: `0.05%`
- Entry window: `2.0` minutes for both `5m` and `15m`
- Min buy price: `0.60`
- Hard max buy price: `0.90`
- Max reprice drift: `0.02`
- Single-leg aggressive `IOC` buy only
- Settle only on actual market resolution

## Runtime modes

### `start-live`

- Uses the same strategy, Kelly sizing, and execution core as paper
- Intended to sit on top of the official Nautilus Polymarket execution path first
- Keeps the live execution boundary swappable if latency forces a thinner custom submit adapter

### `start-paper`

- Always means realistic paper mode
- Uses live feed timing as observed locally
- Simulates only execution-path delay after the strategy decision
- Evaluates the real book at simulated submit time
- Uses actual market resolution for settlement

### `start-dashboard`

- Serves the operator UI plus typed JSON endpoints
- Uses the runtime/config/portfolio state already available locally
- Leaves opportunity/execution-market sections empty until live market adapters are wired
- Keeps unsupported sections honest with explicit dependency notes instead of fake data

## Hot path design

Keep these in one process:

- Polymarket live book/cache
- Coinbase spot feed
- Polymarket RTDS Chainlink feed
- market registry
- divergence strategy
- Kelly sizing
- execution core
- live or realistic-paper execution adapter

Do not put Redis, Postgres, dashboard writes, or REST orderbook fetches in the entry path.

## Realistic paper execution model

`start-paper` uses:

- live Coinbase data
- live RTDS Chainlink data
- live Polymarket books
- Kelly sizing identical to live
- one authoritative in-memory book walk at submit time

Modeled delays:

- internal processing delay
- signing delay
- submit RTT
- ack/user-visibility lag
- confirmation lag
- slow-tail submit events
- restart/`425` blocking behavior

## Remaining work

1. Real Nautilus Polymarket data/execution wiring
2. Rolling market discovery
3. Custom Coinbase spot client
4. Custom Polymarket RTDS client
5. Settlement/resolution workflows
6. Replay/catalog ingestion
7. Live dashboard state, event streaming, and trader tables fed by real runtime data
