# Nautilus Runtime Recovery Plan

Last updated: 2026-04-15

## Goal

Make this repository behave like a real low-latency Polymarket trading system while keeping NautilusTrader as the runtime kernel.

That means:

- one Nautilus `TradingNode`
- one CBCL005 strategy path
- one market-data path
- one runtime state model
- one dashboard/provider path
- two execution clients only:
  - realistic paper
  - live

The runtime should stop diverging between:

- `src/cbcl_platform/nautilus/*`
- `src/cbcl_platform/live/*`
- `src/cbcl_platform/paper_bot.py`

Paper mode must be real-feed paper, not simulator paper. Live mode must reuse the same strategy/runtime shape.

## Non-negotiables

1. Nautilus remains the permanent runtime kernel.
2. Lougetown is a behavioral reference, not a codebase to copy.
3. The operator path must not depend on synthetic market creation, synthetic expiry, or synthetic settlement.
4. Dashboard truth must come from the running Nautilus runtime, not a polling file path, except as an explicit degraded fallback.
5. Live-paper and live must differ only at the execution-client boundary.

## What Lougetown Gets Right

Lougetown is not cleaner than this repo, but it gets several operational behaviors closer to reality:

1. Actionable market selection
   - It tracks current and next relevant crypto minute windows instead of treating discovery as a generic market search problem.
2. Feed watchdog behavior
   - It treats feed health as a state machine, not just a last-age threshold.
3. Coalesced evaluation
   - It evaluates latest joined market state, not every raw event as if each should independently trigger strategy work.
4. Pending-position lifecycle
   - It models pending/open/resolved/settled state transitions more honestly.
5. Provider-backed dashboard behavior
   - The dashboard is closer to the engine than this repo’s persisted-snapshot model.

Those behaviors should be rebuilt inside Nautilus, not beside Nautilus.

## External References

Official references:

- NautilusTrader concepts: adapters, execution, strategies  
  - <https://nautilustrader.io/docs/latest/concepts/adapters/>
  - <https://nautilustrader.io/docs/latest/concepts/execution/>
  - <https://nautilustrader.io/docs/latest/concepts/strategies/>
- NautilusTrader Polymarket integration  
  - <https://nautilustrader.io/docs/latest/integrations/polymarket/>
- Polymarket market channel, user channel, RTDS, matching engine, server time  
  - <https://docs.polymarket.com/market-data/websocket/market-channel>
  - <https://docs.polymarket.com/market-data/websocket/user-channel>
  - <https://docs.polymarket.com/market-data/websocket/rtds>
  - <https://docs.polymarket.com/trading/matching-engine>
  - <https://docs.polymarket.com/api-reference/data/get-server-time>
- Coinbase websocket channels  
  - <https://docs.cdp.coinbase.com/exchange/websocket-feed/channels>

GitHub references worth using:

- Official Python Polymarket CLOB client  
  - <https://github.com/Polymarket/py-clob-client>
- Official Rust Polymarket CLOB client  
  - <https://github.com/Polymarket/rs-clob-client>
- Async Polymarket discovery client with pagination / typed Gamma access  
  - <https://github.com/the-odds-company/aiopolymarket>
- TypeScript websocket client with explicit reconnect / heartbeat handling  
  - <https://github.com/discountry/polymarket-websocket-client>

Use the official docs and official Polymarket repos as the source of truth. Use third-party repos only for implementation patterns.

## Current Repo Diagnosis

### 1. The runtime is only partially Nautilus-native

The operator path now boots a Nautilus node from `src/cbcl_platform/nautilus/node.py`, but core runtime behavior is still split across:

- `src/cbcl_platform/nautilus/*`
- `src/cbcl_platform/live/*`
- `src/cbcl_platform/paper_bot.py`

This repo still behaves like a migrated custom bot rather than a clean Nautilus system.

### 2. Discovery and instrument loading are fighting each other

Current state:

- `MarketRegistry` does custom Gamma/public-search discovery and picks markets.
- Nautilus Polymarket data client is configured with `load_all=True`.
- Strategy subscribes books only after custom binding refresh.

Why this is a problem:

- startup is slower than it should be because Nautilus still initializes a large instrument universe
- discovery truth and Nautilus instrument-provider truth are not the same object graph
- market rollover correctness depends on custom registry timing, not native venue state

### 3. Feed events are recorded twice

Current state:

- `CoinbaseSpotDataClient` and `RtdsChainlinkDataClient` call `services.record_coinbase()` / `services.record_chainlink()`
- then `CBCL005NautilusStrategy.on_data()` reconstructs the same `PriceUpdate` and records it again

Downstream effects:

- duplicated recorder output
- duplicated price-history samples
- inflated “movement” calculations
- less trustworthy monitoring

This must be removed immediately.

### 4. Feed health is too weak

Current state:

- `RuntimeServices` tracks `FeedStatus`
- there is no real feed-health actor or state machine in the Nautilus path
- feed status is mostly age + connected/disconnected

Missing behaviors:

- warmup
- degraded
- symbol-level quiet feed detection
- reconnect-backoff state
- channel-specific incidents
- explicit market book sync state

This is a major reason the system alternates between “nothing happens” and “everything is blocked by skew/stale.”

### 5. Settlement is still polling-driven

Current state:

- `_reconcile_resolutions()` calls `registry.resolve_markets()` on refresh
- no direct use of `market_resolved` events from the market channel

Why this matters:

- settlement lag is at least one refresh interval
- market resolution is not part of the hot runtime event graph
- paper/live parity is weaker than it should be

### 6. The dashboard provider is still only partly fixed

Current state:

- embedded mode reads in-memory runtime state
- separate dashboard process still falls back to persisted snapshot state

This is acceptable for a temporary degraded mode, but not as the target operator architecture.

### 7. Paper execution is the right idea, but not yet the whole Nautilus answer

What is good:

- delayed submit-time book lookup
- realistic fill/reject behavior
- reuse of execution core

What is still missing:

- pending order / pending fill / confirmed fill lifecycle richer than current snapshots
- tighter integration with live execution telemetry and reconciliation semantics

### 8. Live execution still lacks a hard, measured cutover plan

Current state:

- live mode uses the official Nautilus Polymarket execution adapter
- there is no measured decision/sign/submit/ack telemetry gate to decide whether it is fast enough for CBCL005

That means “live ready” is still not defined rigorously.

## Target Architecture

### Shared runtime

- `TradingNode`
- official Nautilus Polymarket public data adapter
- custom Nautilus Coinbase data client
- custom Nautilus RTDS data client
- `CBCL005Strategy`
- health actor/service
- recorder
- dashboard provider
- settlement service

### Mode-specific execution only

- `start-paper` -> `PolymarketPaperExecutionClient`
- `start-live` -> `PolymarketLiveExecutionClient`

### Strategy invariants

CBCL005 must preserve:

- threshold `0.0005`
- `2.0m + 0.25m` entry window
- hard cap `0.90`
- drift cap `0.02`
- taker-only aggressive limit
- hold to actual resolution

### Discovery invariants

Discovery must select:

- BTC + ETH only
- current actionable 5m/15m contracts
- immediate next contract for rollover preparedness

Discovery must not:

- bind large irrelevant market sets
- use “some matching search result” as if it were a production market-selection policy

## Implementation Phases

### Phase 0: Immediate correctness cleanup

Priority: highest
Status: completed on 2026-04-15

Tasks:

1. Remove duplicated feed recording
   - data clients should publish
   - strategy should consume
   - only one layer should write to `RuntimeServices`
2. Remove legacy operator-path confusion
   - demote `src/cbcl_platform/live/*` from the operator path
   - document which files are legacy and which are authoritative
3. Add startup telemetry
   - measure discovery duration
   - measure Nautilus instrument-init duration
   - measure first-usable Coinbase/RTDS/book timestamps

Definition of done:

- no duplicated `coinbase_price` / `chainlink_price` recorder events
- startup logs show how long each phase takes

Implemented:

- removed duplicate Coinbase / Chainlink writes from Nautilus data clients
- kept `RuntimeServices` as the single recorder/history writer for strategy-facing feed state
- added startup metrics for registry bootstrap, node build, total bootstrap, initial bindings, and initial instruments
- added first-usable data timestamps for Coinbase, Chainlink, and Polymarket books

### Phase 1: Make market binding Nautilus-native and actionable

Priority: highest
Status: completed on 2026-04-15

Tasks:

1. Replace “load everything” Polymarket instrument init
   - stop using `PolymarketInstrumentProviderConfig(load_all=True)` as the default path
2. Build a binding service that chooses:
   - current 5m per coin
   - next 5m per coin
   - current 15m per coin
   - next 15m per coin
3. Keep explicit identity separation:
   - market id
   - event slug
   - condition id
   - yes token id
   - no token id
4. Reconcile binding service with Nautilus instrument subscriptions
   - subscribe only bound instruments
   - unsubscribe rolled instruments

Definition of done:

- startup no longer loads a giant irrelevant Polymarket universe
- bound markets are only current/next actionable contracts
- rolled contracts disappear from opportunity state automatically

Implemented:

- replaced default `load_all=True` Polymarket provider boot with `load_ids` for the current actionable binding set
- restricted the paper execution client provider to the same bound instrument set
- added dynamic `request_instrument(...)` and delayed subscription logic so rollover instruments can be loaded on refresh without reintroducing `load_all=True`
- kept opportunity rows pruned to active bindings only

### Phase 2: Add a real feed-health actor

Priority: highest
Status: foundation completed on 2026-04-15; full actor still pending

Tasks:

1. Build a Nautilus-side health component with per-source and per-symbol states:
   - warmup
   - healthy
   - stale
   - degraded
   - reconnecting
   - cooldown-blocked
2. Track separately:
   - Coinbase socket age
   - RTDS socket age
   - selected-book age
   - per-coin feed skew
   - book sync state after reconnect
3. Use market-channel and RTDS heartbeat behavior explicitly
4. Use Polymarket server-time endpoint only for calibration / debugging, not in the hot path

Definition of done:

- dashboard can tell the operator exactly why BTC or ETH is blocked
- stale/skew incidents recover automatically without process restart

Implemented foundation:

- Polymarket market-book feed now emits real feed events into `RuntimeServices`
- runtime snapshots now expose per-feed `state` (`warmup`, `healthy`, `stale`, `reconnecting`) and age
- first-book sync timestamps are now visible in startup telemetry
- skew stale reasons already clear automatically on recovery

Still pending:

- explicit degraded / cooldown-blocked feed states
- dedicated book-sync state machine after reconnect
- richer incident history beyond the current runtime snapshot

### Phase 3: Coalesced evaluation and state joins

Priority: high

Tasks:

1. Keep the dirty-market pattern, but make it the only strategy scheduling path
2. Ensure joined state is evaluated once per dirty market using latest feed state
3. Remove any remaining event handling that writes strategy-facing state in multiple places
4. Make “signals” metrics mean accepted strategy signals only

Definition of done:

- event bursts do not create inflated signal counts
- dashboard metrics are interpretable again

### Phase 4: Resolution and lifecycle correctness

Priority: high

Tasks:

1. Move settlement from polling-first to event-first
   - consume `market_resolved`
   - use REST reconciliation only as backup
2. Expand order lifecycle accounting:
   - submitted
   - accepted
   - partial
   - filled
   - expired
   - rejected
   - confirmed
3. Keep paper and live lifecycle shape identical at the runtime boundary

Definition of done:

- paper and live expose the same lifecycle states
- settlement is no longer delayed until periodic polling

### Phase 5: Paper execution parity

Priority: high

Tasks:

1. Keep the current delayed submit-time book-walk model
2. Add explicit telemetry for:
   - decision time
   - sampled signing delay
   - submit RTT
   - ack delay
   - confirm delay
3. Persist expected vs actual paper quote outcomes for replay

Definition of done:

- paper mode can explain every fill or reject from real market state + modeled execution delay

### Phase 6: Live execution enablement

Priority: high

Tasks:

1. Start with official Nautilus Polymarket execution adapter
2. Add live measurement gates:
   - decision latency
   - sign latency
   - submit RTT
   - ack latency
   - fill quality vs expected book
3. If too slow, keep Nautilus runtime and replace only the live execution adapter path
4. Use `Polymarket/rs-clob-client` as the primary reference for a future low-latency live adapter or sidecar

Definition of done:

- live cutover is based on telemetry, not opinion

### Phase 7: Dashboard and provider completion

Priority: medium

Tasks:

1. Keep embedded dashboard as the default operator mode
2. Add a proper node-owned control-plane API for separate-process dashboards
3. Degrade to persisted snapshots only when explicitly disconnected from the live node
4. Keep the top-of-screen market monitor always visible

Definition of done:

- operator always sees BTC/ETH movement, divergence, skew, and freshness
- dashboard truth is not dependent on file polling in normal operation

## Validation Matrix

Every phase must include:

1. unit tests
2. smoke runtime test
3. dashboard API verification
4. recorder verification

Required checks:

- `ruff check src tests`
- focused pytest for each touched subsystem
- `start-paper --with-dashboard` smoke
- API checks:
  - `/healthz`
  - `/api/bootstrap`
  - `/api/opportunities`
  - `/api/system`

For live readiness:

- shadow-mode telemetry review
- micro-notional live run
- compare expected submit-time book vs actual fills

## Internal Working Plan

This is the concrete work queue to execute in order.

### Step 1

Stabilize the current Nautilus path:

- remove duplicate feed recording
- remove stale legacy operator code from the active path
- instrument startup timing

### Step 2

Replace the current discovery path:

- current/next BTC/ETH 5m/15m only
- no `load_all=True` production default
- clean rollover semantics

### Step 3

Add a real feed-health actor:

- socket health
- symbol health
- book sync health
- skew health

### Step 4

Make settlement event-driven:

- `market_resolved` first
- REST fallback second

### Step 5

Strengthen paper lifecycle:

- submitted / accepted / partial / filled / expired / confirmed
- replayable telemetry for every transition

### Step 6

Establish live execution decision gates:

- official Nautilus live adapter first
- if too slow, replace only the submit/sign adapter path

### Step 7

Finish the provider/dashboard path:

- embedded dashboard default
- node-owned API for external dashboard mode

## Success Criteria

This plan is complete only when:

1. `start-paper` is a real-feed Nautilus paper runtime
2. `start-live` uses the same runtime shape and strategy path
3. market binding is current/next and rolls cleanly
4. feed health is explicit and self-recovering
5. settlement is event-driven
6. dashboard always shows BTC/ETH monitor state, even with no active trade
7. live readiness is measured from telemetry

## Current Recommended Next Actions

1. Remove the double `record_coinbase()` / `record_chainlink()` path.
2. Replace `load_all=True` Polymarket startup with actionable-bound instrument init.
3. Build the feed-health actor and move all stale/skew/book-sync state into it.
4. Move settlement from polling-first to `market_resolved`-first.
5. Add live execution telemetry gates before any serious live deployment.
