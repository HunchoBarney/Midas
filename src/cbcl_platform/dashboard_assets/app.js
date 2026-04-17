const state = {
  page: "overview",
  theme: localStorage.getItem("cbcl-theme") || "light",
  bootstrap: null,
  loading: false,
  error: "",
  refreshInFlight: false,
};

const FOREGROUND_REFRESH_MS = 1000;
const BACKGROUND_REFRESH_MS = 5000;
let refreshTimer = 0;

const NAV_META = {
  overview: { label: "Terminal", pill: "Live" },
  execution: { label: "Trades" },
  portfolio: { label: "Positions" },
  system: { label: "Runtime" },
};

const $ = (selector) => document.querySelector(selector);

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function badge(text, tone = "neutral") {
  return `<span class="badge tone-${tone}">${esc(text)}</span>`;
}

function toneClass(level) {
  const normalized = String(level || "neutral").toLowerCase();
  if (normalized === "good" || normalized === "implemented" || normalized === "paper") {
    return "good";
  }
  if (
    normalized === "warning" ||
    normalized === "warn" ||
    normalized === "blocked" ||
    normalized === "planned" ||
    normalized === "live"
  ) {
    return "warning";
  }
  if (normalized === "danger" || normalized === "bad") return "danger";
  return "neutral";
}

function fmtMoney(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: num >= 100 ? 2 : 4,
    maximumFractionDigits: num >= 100 ? 2 : 4,
  }).format(num);
}

function fmtValue(value) {
  if (value === null || value === undefined || value === "") return "--";
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? String(value)
      : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function fmtMs(value) {
  if (value === null || value === undefined || value === "") return "--";
  return `${fmtValue(value)}ms`;
}

function fmtPct(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "--";
  return `${num.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")}%`;
}

function averageOf(rows, key) {
  const values = rows
    .map((row) => Number(row[key]))
    .filter((value) => Number.isFinite(value) && value > 0);
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function executionData() {
  return state.bootstrap?.execution || {
    orders: [],
    fills: [],
    rejects: [],
    settlements: [],
    delay_model: {
      internal_ms: { p50_ms: 0, p95_ms: 0, p99_ms: 0 },
      signing_ms: { p50_ms: 0, p95_ms: 0, p99_ms: 0 },
      submit_rtt_ms: { p50_ms: 0, p95_ms: 0, p99_ms: 0 },
      ack_delay_ms: { p50_ms: 0, p95_ms: 0, p99_ms: 0 },
      confirmed_min_ms: 0,
      confirmed_max_ms: 0,
    },
    policy: {
      order_type: "--",
      hard_cap: 0,
      max_price_drift: 0,
      partial_fills_allowed: false,
    },
    notes: [],
  };
}

function portfolioData() {
  return state.bootstrap?.portfolio || {
    summary: {
      cash_balance_usd: 0,
      total_exposure_usd: 0,
      realized_pnl_usd: 0,
      open_positions: 0,
      mode_note: "",
    },
    positions: [],
    allocations: [],
    empty_state: "No positions.",
  };
}

function systemData() {
  return state.bootstrap?.system || {
    runtime: { mode: "--", environment: "--", markets: [], data_stack: [], execution_stack: [] },
    paper_loop: { status: "idle", loop: {}, metrics: {} },
    components: [],
    commands: [],
  };
}

function derivedPositionRows(portfolio) {
  return (portfolio.positions || []).map((row) => {
    const yesShares = Number(row.yes_shares || 0);
    const noShares = Number(row.no_shares || 0);
    const side = yesShares > 0 ? "YES" : "NO";
    const shares = yesShares > 0 ? yesShares : noShares;
    const costBasis = Number(row.cost_basis_usd || 0);
    return {
      market_id: row.market_id,
      side,
      shares,
      avg_entry: shares > 0 ? costBasis / shares : null,
      cost_basis_usd: costBasis,
      fees_paid_usd: Number(row.fees_paid_usd || 0),
    };
  });
}

async function fetchBootstrap({ background = false } = {}) {
  if (state.refreshInFlight) return;
  state.refreshInFlight = true;
  state.loading = !background || !state.bootstrap;
  state.error = "";
  renderApp();
  try {
    const response = await fetch("/api/bootstrap", { cache: "no-store" });
    if (!response.ok) throw new Error("bootstrap request failed");
    state.bootstrap = await response.json();
  } catch (error) {
    state.error = error instanceof Error ? error.message : "dashboard load failed";
  } finally {
    state.refreshInFlight = false;
    state.loading = false;
    renderApp();
  }
}

function refreshIntervalMs() {
  return document.visibilityState === "hidden"
    ? BACKGROUND_REFRESH_MS
    : FOREGROUND_REFRESH_MS;
}

function scheduleRefreshLoop() {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
  refreshTimer = window.setInterval(() => {
    void fetchBootstrap({ background: true });
  }, refreshIntervalMs());
}

function renderNav() {
  const navigation = state.bootstrap?.navigation || [];
  return navigation
    .map((item) => {
      const meta = NAV_META[item.id] || {};
      const active = item.id === state.page ? "active" : "";
      return `
        <button class="nav-btn ${active}" data-page="${esc(item.id)}">
          <span>${esc(meta.label || item.label)}</span>
          ${meta.pill ? `<span class="nav-pill">${esc(meta.pill)}</span>` : ""}
        </button>
      `;
    })
    .join("");
}

function renderSidebarStatus() {
  const overview = state.bootstrap?.overview;
  if (!overview) return "";
  const metrics = overview.metrics || [];
  const findMetric = (label) => metrics.find((item) => item.label === label)?.value || "--";
  return `
    <div class="sidebar-card">
      <h3>Runtime</h3>
      <div class="stack">
        <div class="status-pill">${esc(overview.mode.toUpperCase())}</div>
        <div class="status-pill mono">${esc(overview.environment)}</div>
        <div class="status-pill mono">Equity ${esc(findMetric("Equity"))}</div>
        <div class="status-pill mono">Pos ${esc(findMetric("Open positions"))} | Orders ${esc(findMetric("Orders"))}</div>
      </div>
    </div>
  `;
}

function renderTopbar() {
  const overview = state.bootstrap?.overview;
  const generatedAt = state.bootstrap?.generated_at || "--";
  const mode = overview?.mode || "--";
  return `
    <header class="topbar">
      <div>
        <span class="mode-pill">
          <span class="dot ${toneClass(mode)}"></span>
          <span>${esc(mode.toUpperCase())}</span>
        </span>
      </div>
      <div class="topbar-actions">
        <span class="status-pill mono">${esc(overview?.environment || "--")}</span>
        <span class="status-pill mono">${esc((overview?.markets || []).join(", ") || "--")}</span>
        <span class="status-pill mono">Updated ${esc(generatedAt)}</span>
        <button class="btn" id="refresh-btn">Refresh</button>
        <button class="btn" id="theme-btn">Theme: ${state.theme === "dark" ? "Dark" : "Light"}</button>
      </div>
    </header>
  `;
}

function renderSummaryStrip() {
  const metrics = state.bootstrap?.overview?.metrics || [];
  return `
    <section class="metric-row">
      ${metrics
        .map(
          (metric) => `
            <article class="metric">
              <div class="k">${esc(metric.label)}</div>
              <div class="v">${esc(metric.value)}</div>
              <div class="metric-detail">${esc(metric.detail)}</div>
            </article>
          `,
        )
        .join("")}
    </section>
  `;
}

function monitorTone(row) {
  const freshness = String(row?.freshness || "").toLowerCase();
  const marketState = String(row?.market_state || "").toLowerCase();
  if (freshness.includes("missing") || freshness.includes("stale")) return "warning";
  if (marketState.includes("awaiting") || marketState.includes("unbound")) return "neutral";
  return "good";
}

function renderMonitorStrip() {
  const opportunities = state.bootstrap?.opportunities || {};
  const rows = opportunities.monitor_rows || [];
  if (!rows.length) return "";
  return `
    <section class="pulse-grid">
      ${rows
        .map(
          (row) => `
            <article class="page-card pulse-card">
              <div class="pulse-head">
                <div>
                  <div class="pulse-title">${esc(row.coin || "--")}</div>
                  <div class="pulse-subtitle">${esc(row.active_market || "No bound market")}</div>
                </div>
                ${badge(row.market_state || "monitor", toneClass(monitorTone(row)))}
              </div>
              <div class="pulse-metrics">
                <div class="pulse-metric">
                  <div class="pulse-label">Spot</div>
                  <div class="pulse-value mono">${esc(fmtValue(row.spot_price))}</div>
                </div>
                <div class="pulse-metric">
                  <div class="pulse-label">Oracle</div>
                  <div class="pulse-value mono">${esc(fmtValue(row.oracle_price))}</div>
                </div>
                <div class="pulse-metric">
                  <div class="pulse-label">Divergence</div>
                  <div class="pulse-value mono">${esc(fmtPct(row.divergence_pct))}</div>
                </div>
                <div class="pulse-metric">
                  <div class="pulse-label">Spot 1m</div>
                  <div class="pulse-value mono">${esc(fmtPct(row.spot_move_1m_pct))}</div>
                </div>
                <div class="pulse-metric">
                  <div class="pulse-label">Oracle 1m</div>
                  <div class="pulse-value mono">${esc(fmtPct(row.oracle_move_1m_pct))}</div>
                </div>
                <div class="pulse-metric">
                  <div class="pulse-label">Volume 24h</div>
                  <div class="pulse-value mono">${esc(fmtValue(row.volume_24h))}</div>
                </div>
              </div>
              <div class="pulse-foot">
                <span class="pulse-chip mono">Freshness ${esc(row.freshness || "--")}</span>
                <span class="pulse-chip mono">Skew ${esc(fmtMs(row.feed_skew_ms))}</span>
                <span class="pulse-chip mono">Close ${esc(fmtValue(row.minutes_to_close))}m</span>
              </div>
            </article>
          `,
        )
        .join("")}
    </section>
  `;
}

function renderAlerts() {
  const alerts = state.bootstrap?.overview?.alerts || [];
  return `
    <article class="rail-card">
      <h3>Alerts</h3>
      <div class="alert-list">
        ${alerts
          .map(
            (alert) => `
              <div class="callout">
                ${badge(alert.title, toneClass(alert.level))}
                <div class="alert-detail" style="margin-top:10px">${esc(alert.detail)}</div>
              </div>
            `,
          )
          .join("")}
      </div>
    </article>
  `;
}

function renderRuntimeRail() {
  const system = systemData();
  const loop = system.paper_loop?.loop || {};
  const metrics = system.paper_loop?.metrics || {};
  const portfolio = portfolioData();
  return `
    <article class="rail-card">
      <h3>Bot Status</h3>
      <div class="kv-list">
        <div class="kv-row"><div class="kv-key">Loop</div><div class="kv-value mono">${esc(system.paper_loop?.status || "--")}</div></div>
        <div class="kv-row"><div class="kv-key">Signals</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_seen))}</div></div>
        <div class="kv-row"><div class="kv-key">Signals attempted</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_accepted))}</div></div>
        <div class="kv-row"><div class="kv-key">Orders</div><div class="kv-value mono">${esc(fmtValue(metrics.orders_submitted))}</div></div>
        <div class="kv-row"><div class="kv-key">Fills</div><div class="kv-value mono">${esc(fmtValue(metrics.fills))}</div></div>
        <div class="kv-row"><div class="kv-key">Rejects</div><div class="kv-value mono">${esc(fmtValue(metrics.rejections))}</div></div>
        <div class="kv-row"><div class="kv-key">Blocked (open)</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_blocked_open_position))}</div></div>
        <div class="kv-row"><div class="kv-key">Blocked (cooldown)</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_blocked_cooldown))}</div></div>
        <div class="kv-row"><div class="kv-key">Win rate</div><div class="kv-value mono">${esc(`${fmtValue(metrics.win_rate_pct || 0)}%`)}</div></div>
        <div class="kv-row"><div class="kv-key">Realized PnL</div><div class="kv-value mono">${esc(fmtMoney(portfolio.summary.realized_pnl_usd))}</div></div>
        <div class="kv-row"><div class="kv-key">Tick avg</div><div class="kv-value mono">${esc(fmtMs(loop.avg_tick_ms))}</div></div>
      </div>
    </article>
  `;
}

function renderLastEventRail(title, row, tone) {
  if (!row) {
    return `
      <article class="rail-card">
        <h3>${esc(title)}</h3>
        <div class="empty-state">No rows yet.</div>
      </article>
    `;
  }
  return `
    <article class="rail-card">
      <h3>${esc(title)}</h3>
      <div class="callout">
        ${badge(row.status || row.reason || title, tone)}
        <div class="kv-list" style="margin-top:10px">
          <div class="kv-row"><div class="kv-key">Market</div><div class="kv-value">${esc(row.market || row.market_id || "--")}</div></div>
          <div class="kv-row"><div class="kv-key">Side</div><div class="kv-value mono">${esc(row.side || "--")}</div></div>
          <div class="kv-row"><div class="kv-key">Price</div><div class="kv-value mono">${esc(fmtValue(row.average_price || row.limit_price))}</div></div>
          <div class="kv-row"><div class="kv-key">Shares</div><div class="kv-value mono">${esc(fmtValue(row.filled_shares || row.requested_shares))}</div></div>
          <div class="kv-row"><div class="kv-key">Time</div><div class="kv-value mono">${esc(row.time || "--")}</div></div>
        </div>
      </div>
    </article>
  `;
}

function renderTableCard({ title, subtitle = "", rows, columns, emptyState = "No rows yet." }) {
  if (!rows.length) {
    return `
      <article class="page-card">
        <div class="card-head">
          <div>
            <h2 class="card-title">${esc(title)}</h2>
            ${subtitle ? `<p class="card-copy">${esc(subtitle)}</p>` : ""}
          </div>
        </div>
        <div class="table-wrap"><div class="table-empty">${esc(emptyState)}</div></div>
      </article>
    `;
  }
  return `
    <article class="page-card">
      <div class="card-head">
        <div>
          <h2 class="card-title">${esc(title)}</h2>
          ${subtitle ? `<p class="card-copy">${esc(subtitle)}</p>` : ""}
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>${columns.map((column) => `<th>${esc(column.label)}</th>`).join("")}</tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (row) => `
                  <tr>
                    ${columns
                      .map((column) => {
                        const value = row[column.key];
                        let rendered = fmtValue(value);
                        if (column.format === "money") rendered = fmtMoney(value);
                        if (column.format === "ms") rendered = fmtMs(value);
                        if (column.format === "pct") rendered = fmtPct(value);
                        return `<td class="${column.mono ? "mono" : ""}">${esc(rendered)}</td>`;
                      })
                      .join("")}
                  </tr>
                `,
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </article>
  `;
}

function renderDelayCard(title, percentiles, maxWidth) {
  const rows = [
    ["P50", percentiles.p50_ms],
    ["P95", percentiles.p95_ms],
    ["P99", percentiles.p99_ms],
  ];
  return `
    <div class="delay-card">
      <div class="metric-label">${esc(title)}</div>
      <div class="delay-bars">
        ${rows
          .map(([label, value]) => {
            const width = maxWidth > 0 ? Math.max(8, (Number(value) / maxWidth) * 100) : 0;
            return `
              <div class="delay-row">
                <span class="mono">${esc(label)}</span>
                <div class="delay-track"><div class="delay-fill" style="width:${width}%"></div></div>
                <span class="mono">${esc(value)}ms</span>
              </div>
            `;
          })
          .join("")}
      </div>
    </div>
  `;
}

function renderExecutionSummary() {
  const execution = executionData();
  const orders = execution.orders || [];
  const fills = execution.fills || [];
  const rejects = execution.rejects || [];
  const avgSubmit = averageOf(orders, "submit_ms");
  const avgAck = averageOf(orders, "ack_ms");
  return `
    <section class="summary-strip" style="grid-template-columns:repeat(5,minmax(0,1fr))">
      <article class="metric-card">
        <div class="metric-label">Attempts</div>
        <div class="metric-value">${esc(fmtValue(orders.length))}</div>
      </article>
      <article class="metric-card">
        <div class="metric-label">Fills</div>
        <div class="metric-value">${esc(fmtValue(fills.length))}</div>
      </article>
      <article class="metric-card">
        <div class="metric-label">Rejects</div>
        <div class="metric-value">${esc(fmtValue(rejects.length))}</div>
      </article>
      <article class="metric-card">
        <div class="metric-label">Avg submit</div>
        <div class="metric-value">${esc(fmtMs(avgSubmit))}</div>
      </article>
      <article class="metric-card">
        <div class="metric-label">Avg ack</div>
        <div class="metric-value">${esc(fmtMs(avgAck))}</div>
      </article>
    </section>
  `;
}

function renderOverviewPage() {
  const overview = state.bootstrap.overview;
  const opportunities = state.bootstrap.opportunities;
  const execution = executionData();
  const portfolio = portfolioData();
  const positions = derivedPositionRows(portfolio);

  return `
    <div class="page-stack">
      <section class="page-card hero-card">
        <div>
          <div class="section-hd">
            <h2>Trading Terminal</h2>
            <span class="tag mono">${esc(overview.hero.secondary_value)}</span>
          </div>
          <div class="hero-summary">${esc(overview.hero.summary)}</div>
          <div class="hero-summary" style="margin-top:10px">
            Focused on what matters: positions, fills, orders, rejects, and current opportunity state.
          </div>
        </div>
        <div class="meta-grid">
          <div class="meta-card">
            <div class="metric-label">Runtime</div>
            <div class="metric-value mono">${esc(overview.mode.toUpperCase())}</div>
          </div>
          <div class="meta-card">
            <div class="metric-label">Execution</div>
            <div class="metric-value">${esc(execution.policy.order_type)}</div>
          </div>
          <div class="meta-card">
            <div class="metric-label">Hard cap</div>
            <div class="metric-value mono">${esc(fmtValue(execution.policy.hard_cap))}</div>
          </div>
          <div class="meta-card">
            <div class="metric-label">Drift cap</div>
            <div class="metric-value mono">${esc(fmtValue(execution.policy.max_price_drift))}</div>
          </div>
        </div>
      </section>

      ${renderTableCard({
        title: "Market monitor",
        subtitle: "BTC/ETH live feed state stays visible even when no tradable UpDown contract is currently bound.",
        rows: opportunities.monitor_rows || [],
        emptyState:
          opportunities.monitor_summary ||
          "Market monitor rows will populate when live BTC/ETH feed state is available.",
        columns: [
          { key: "coin", label: "Coin", mono: true },
          { key: "active_market", label: "Bound market" },
          { key: "spot_price", label: "Coinbase spot" },
          { key: "oracle_price", label: "Chainlink" },
          { key: "divergence_pct", label: "Divergence", format: "pct" },
          { key: "spot_move_1m_pct", label: "Spot 1m", format: "pct" },
          { key: "oracle_move_1m_pct", label: "Oracle 1m", format: "pct" },
          { key: "volume_24h", label: "Volume 24h" },
          { key: "feed_skew_ms", label: "Skew", format: "ms" },
          { key: "freshness", label: "Freshness", mono: true },
          { key: "market_state", label: "State" },
        ],
      })}

      <section class="split-grid">
        ${renderTableCard({
          title: "Open positions",
          subtitle: "What the bot is currently holding.",
          rows: positions,
          emptyState: portfolio.empty_state,
          columns: [
            { key: "market_id", label: "Market", mono: true },
            { key: "side", label: "Side", mono: true },
            { key: "shares", label: "Shares" },
            { key: "avg_entry", label: "Avg entry" },
            { key: "cost_basis_usd", label: "Cost basis", format: "money" },
            { key: "fees_paid_usd", label: "Fees", format: "money" },
          ],
        })}
        ${renderTableCard({
          title: "Recent fills",
          subtitle: "Trades the bot actually got filled on.",
          rows: execution.fills.slice(0, 8),
          columns: [
            { key: "time", label: "Time", mono: true },
            { key: "market", label: "Market" },
            { key: "side", label: "Side", mono: true },
            { key: "filled_shares", label: "Shares" },
            { key: "average_price", label: "Fill px" },
            { key: "total_cost", label: "Cost", format: "money" },
            { key: "confirm_ms", label: "Confirm", format: "ms" },
          ],
        })}
      </section>

      <section class="split-grid">
        ${renderTableCard({
          title: "Recent order attempts",
          subtitle: "Every trade attempt, whether it filled or not.",
          rows: execution.orders.slice(0, 8),
          columns: [
            { key: "time", label: "Time", mono: true },
            { key: "market", label: "Market" },
            { key: "side", label: "Side", mono: true },
            { key: "status", label: "Status", mono: true },
            { key: "limit_price", label: "Limit px" },
            { key: "filled_shares", label: "Filled" },
            { key: "submit_ms", label: "Submit", format: "ms" },
            { key: "reason", label: "Reason" },
          ],
        })}
        ${renderTableCard({
          title: "Recent rejects",
          subtitle: "Why attempted trades were turned away.",
          rows: execution.rejects.slice(0, 8),
          columns: [
            { key: "time", label: "Time", mono: true },
            { key: "market", label: "Market" },
            { key: "side", label: "Side", mono: true },
            { key: "limit_price", label: "Limit px" },
            { key: "submit_ms", label: "Submit", format: "ms" },
            { key: "reason", label: "Reason" },
          ],
        })}
      </section>

      ${renderTableCard({
        title: "Opportunity board",
        subtitle: "Current market opportunities ranked by readiness and divergence.",
        rows: (opportunities.rows || []).slice(0, 12),
        emptyState: opportunities.summary,
        columns: (opportunities.columns || []).map((column) => ({ key: column, label: column })),
      })}
    </div>
  `;
}

function renderExecutionPage() {
  const execution = executionData();
  const orders = execution.orders || [];
  const fills = execution.fills || [];
  const rejects = execution.rejects || [];
  const settlements = execution.settlements || [];
  const maxDelay = Math.max(
    execution.delay_model.internal_ms.p99_ms,
    execution.delay_model.signing_ms.p99_ms,
    execution.delay_model.submit_rtt_ms.p99_ms,
    execution.delay_model.ack_delay_ms.p99_ms,
  );
  return `
    <div class="page-stack">
      ${renderExecutionSummary()}
      ${renderTableCard({
        title: "Trade attempts",
        subtitle: "Everything the bot tried to do in the market.",
        rows: orders,
        columns: [
          { key: "time", label: "Time", mono: true },
          { key: "market", label: "Market" },
          { key: "side", label: "Side", mono: true },
          { key: "status", label: "Status", mono: true },
          { key: "limit_price", label: "Limit px" },
          { key: "filled_shares", label: "Filled" },
          { key: "average_price", label: "Fill px" },
          { key: "submit_ms", label: "Submit", format: "ms" },
          { key: "ack_ms", label: "Ack", format: "ms" },
          { key: "reason", label: "Reason" },
        ],
      })}
      <section class="split-grid">
        ${renderTableCard({
          title: "Fills",
          subtitle: "Successful executions only.",
          rows: fills,
          columns: [
            { key: "time", label: "Time", mono: true },
            { key: "market", label: "Market" },
            { key: "side", label: "Side", mono: true },
            { key: "filled_shares", label: "Shares" },
            { key: "average_price", label: "Fill px" },
            { key: "total_cost", label: "Cost", format: "money" },
            { key: "trade_fee_usd", label: "Fee", format: "money" },
            { key: "confirm_ms", label: "Confirm", format: "ms" },
          ],
        })}
        ${renderTableCard({
          title: "Rejects",
          subtitle: "Rejected execution attempts.",
          rows: rejects,
          columns: [
            { key: "time", label: "Time", mono: true },
            { key: "market", label: "Market" },
            { key: "side", label: "Side", mono: true },
            { key: "limit_price", label: "Limit px" },
            { key: "submit_ms", label: "Submit", format: "ms" },
            { key: "reason", label: "Reason" },
          ],
        })}
      </section>
      ${renderTableCard({
        title: "Settlements",
        subtitle: "Resolved positions and realized outcomes.",
        rows: settlements,
        columns: [
          { key: "time", label: "Time", mono: true },
          { key: "market_id", label: "Market", mono: true },
          { key: "coin", label: "Coin" },
          { key: "interval", label: "Window" },
          { key: "winning_side", label: "Winner" },
          { key: "pnl_usd", label: "PnL", format: "money" },
        ],
      })}
      <section class="page-card">
        <div class="card-head">
          <div>
            <h2 class="card-title">Execution timing model</h2>
            <p class="card-copy">Paper mode latency stages used to judge submit-time fills.</p>
          </div>
        </div>
        <div class="delay-grid">
          ${renderDelayCard("Internal", execution.delay_model.internal_ms, maxDelay)}
          ${renderDelayCard("Signing", execution.delay_model.signing_ms, maxDelay)}
          ${renderDelayCard("Submit RTT", execution.delay_model.submit_rtt_ms, maxDelay)}
          ${renderDelayCard("Ack delay", execution.delay_model.ack_delay_ms, maxDelay)}
        </div>
      </section>
    </div>
  `;
}

function renderPortfolioPage() {
  const portfolio = portfolioData();
  const positions = derivedPositionRows(portfolio);
  return `
    <div class="page-stack">
      <section class="page-card">
        <div class="card-head">
          <div>
            <h2 class="card-title">Portfolio</h2>
            <p class="card-copy">${esc(portfolio.summary.mode_note)}</p>
          </div>
        </div>
        <div class="summary-strip" style="grid-template-columns:repeat(4,minmax(0,1fr))">
          <article class="metric-card">
            <div class="metric-label">Cash balance</div>
            <div class="metric-value">${esc(fmtMoney(portfolio.summary.cash_balance_usd))}</div>
          </article>
          <article class="metric-card">
            <div class="metric-label">Exposure</div>
            <div class="metric-value">${esc(fmtMoney(portfolio.summary.total_exposure_usd))}</div>
          </article>
          <article class="metric-card">
            <div class="metric-label">Realized PnL</div>
            <div class="metric-value">${esc(fmtMoney(portfolio.summary.realized_pnl_usd))}</div>
          </article>
          <article class="metric-card">
            <div class="metric-label">Open positions</div>
            <div class="metric-value">${esc(fmtValue(portfolio.summary.open_positions))}</div>
          </article>
        </div>
      </section>
      ${renderTableCard({
        title: "Positions",
        subtitle: "Current inventory with entry and exposure detail.",
        rows: positions,
        emptyState: portfolio.empty_state,
        columns: [
          { key: "market_id", label: "Market", mono: true },
          { key: "side", label: "Side", mono: true },
          { key: "shares", label: "Shares" },
          { key: "avg_entry", label: "Avg entry" },
          { key: "cost_basis_usd", label: "Cost basis", format: "money" },
          { key: "fees_paid_usd", label: "Fees", format: "money" },
        ],
      })}
      ${renderTableCard({
        title: "Exposure allocation",
        subtitle: "How deployed capital is distributed across markets.",
        rows: portfolio.allocations || [],
        columns: [
          { key: "market_id", label: "Market", mono: true },
          { key: "share_pct", label: "Share %" },
          { key: "cost_basis_usd", label: "Cost basis", format: "money" },
        ],
      })}
    </div>
  `;
}

function renderSystemPage() {
  const system = systemData();
  const loop = system.paper_loop?.loop || {};
  const metrics = system.paper_loop?.metrics || {};
  const execution = executionData();
  const settings = state.bootstrap?.settings || {};
  const maxDelay = Math.max(
    execution.delay_model.internal_ms.p99_ms,
    execution.delay_model.signing_ms.p99_ms,
    execution.delay_model.submit_rtt_ms.p99_ms,
    execution.delay_model.ack_delay_ms.p99_ms,
  );
  return `
    <div class="page-stack">
      <section class="split-grid">
        <article class="page-card">
          <div class="card-head">
            <div>
              <h2 class="card-title">Loop health</h2>
            </div>
          </div>
          <div class="kv-list">
            <div class="kv-row"><div class="kv-key">Status</div><div class="kv-value mono">${esc(system.paper_loop?.status || "--")}</div></div>
            <div class="kv-row"><div class="kv-key">Feed mode</div><div class="kv-value mono">${esc(system.paper_loop?.feed_mode || "--")}</div></div>
            <div class="kv-row"><div class="kv-key">Signals seen</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_seen))}</div></div>
            <div class="kv-row"><div class="kv-key">Signals attempted</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_accepted))}</div></div>
            <div class="kv-row"><div class="kv-key">Blocked (open pos)</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_blocked_open_position))}</div></div>
            <div class="kv-row"><div class="kv-key">Blocked (cooldown)</div><div class="kv-value mono">${esc(fmtValue(metrics.signals_blocked_cooldown))}</div></div>
            <div class="kv-row"><div class="kv-key">Orders submitted</div><div class="kv-value mono">${esc(fmtValue(metrics.orders_submitted))}</div></div>
            <div class="kv-row"><div class="kv-key">Tick avg</div><div class="kv-value mono">${esc(fmtMs(loop.avg_tick_ms))}</div></div>
            <div class="kv-row"><div class="kv-key">Tick max</div><div class="kv-value mono">${esc(fmtMs(loop.max_tick_ms))}</div></div>
          </div>
        </article>
        <article class="page-card">
          <div class="card-head">
            <div>
              <h2 class="card-title">Runtime stack</h2>
            </div>
          </div>
          <div class="kv-list">
            <div class="kv-row"><div class="kv-key">Mode</div><div class="kv-value mono">${esc(system.runtime.mode)}</div></div>
            <div class="kv-row"><div class="kv-key">Environment</div><div class="kv-value mono">${esc(system.runtime.environment)}</div></div>
            <div class="kv-row"><div class="kv-key">Markets</div><div class="kv-value mono">${esc((system.runtime.markets || []).join(", "))}</div></div>
            <div class="kv-row"><div class="kv-key">Data stack</div><div class="kv-value">${esc((system.runtime.data_stack || []).join(", "))}</div></div>
            <div class="kv-row"><div class="kv-key">Execution stack</div><div class="kv-value">${esc((system.runtime.execution_stack || []).join(", "))}</div></div>
            <div class="kv-row"><div class="kv-key">State path</div><div class="kv-value mono">${esc(system.paper_loop?.state_path || "--")}</div></div>
          </div>
        </article>
      </section>
      <section class="page-card">
        <div class="card-head">
          <div>
            <h2 class="card-title">Execution latency model</h2>
            <p class="card-copy">Paper mode timing assumptions kept available here, not in the main trading view.</p>
          </div>
        </div>
        <div class="delay-grid">
          ${renderDelayCard("Internal", execution.delay_model.internal_ms, maxDelay)}
          ${renderDelayCard("Signing", execution.delay_model.signing_ms, maxDelay)}
          ${renderDelayCard("Submit RTT", execution.delay_model.submit_rtt_ms, maxDelay)}
          ${renderDelayCard("Ack delay", execution.delay_model.ack_delay_ms, maxDelay)}
        </div>
      </section>
      <section class="split-grid">
        <article class="page-card">
          <div class="card-head">
            <div>
              <h2 class="card-title">Current config</h2>
            </div>
          </div>
          <div class="kv-list">
            <div class="kv-row"><div class="kv-key">Strategy</div><div class="kv-value mono">${esc(settings.strategy?.strategy_name || "--")}</div></div>
            <div class="kv-row"><div class="kv-key">Threshold</div><div class="kv-value mono">${esc(fmtValue(settings.strategy?.threshold))}</div></div>
            <div class="kv-row"><div class="kv-key">Signal min buy</div><div class="kv-value mono">${esc(fmtValue(settings.strategy?.signal_min_buy_price ?? settings.strategy?.min_buy_price))}</div></div>
            <div class="kv-row"><div class="kv-key">Signal max buy</div><div class="kv-value mono">${esc(fmtValue(settings.strategy?.signal_max_buy_price))}</div></div>
            <div class="kv-row"><div class="kv-key">Hard cap</div><div class="kv-value mono">${esc(fmtValue(settings.strategy?.hard_cap ?? settings.strategy?.max_buy_price))}</div></div>
            <div class="kv-row"><div class="kv-key">Signal selected-book freshness</div><div class="kv-value mono">${esc(String(settings.strategy?.signal_require_fresh_selected_book ?? "--"))}</div></div>
            <div class="kv-row"><div class="kv-key">Max drift</div><div class="kv-value mono">${esc(fmtValue(settings.strategy?.max_price_drift))}</div></div>
            <div class="kv-row"><div class="kv-key">Kelly fraction</div><div class="kv-value mono">${esc(fmtValue(settings.kelly?.fraction))}</div></div>
          </div>
        </article>
        <article class="page-card">
          <div class="card-head">
            <div>
              <h2 class="card-title">Pending integrations</h2>
            </div>
          </div>
          <div class="status-list">
            ${system.components
              .filter((component) => component.status !== "implemented")
              .map(
                (component) => `
                  <div class="status-row">
                    <div class="status-main">
                      <div class="status-name">${esc(component.component)}</div>
                      <div class="status-detail">${esc(component.detail)}</div>
                    </div>
                    ${badge(component.status, toneClass(component.tone))}
                  </div>
                `,
              )
              .join("")}
          </div>
        </article>
      </section>
    </div>
  `;
}

function renderPage() {
  if (!state.bootstrap) return "";
  if (state.page === "execution") return renderExecutionPage();
  if (state.page === "portfolio") return renderPortfolioPage();
  if (state.page === "system") return renderSystemPage();
  return renderOverviewPage();
}

function renderApp() {
  document.body.setAttribute("data-theme", state.theme);
  localStorage.setItem("cbcl-theme", state.theme);

  if (state.error) {
    $("#app").innerHTML = `
      <div class="app-shell">
        <section class="workspace">
          <section class="page-card">
            <div class="section-hd"><h2>Terminal Error</h2></div>
            <p class="card-copy">${esc(state.error)}</p>
            <button class="btn" id="retry-btn">Retry</button>
          </section>
        </section>
      </div>
    `;
    $("#retry-btn")?.addEventListener("click", fetchBootstrap);
    return;
  }

  if (state.loading && !state.bootstrap) {
    $("#app").innerHTML = `
      <div class="app-shell">
        <section class="workspace">
          <section class="page-card">
            <div class="section-hd"><h2>Loading Terminal</h2></div>
            <p class="card-copy">Building the operator snapshot from the current runtime...</p>
          </section>
        </section>
      </div>
    `;
    return;
  }

  const execution = executionData();
  $("#app").innerHTML = `
    <div class="app-shell">
      <aside class="sidebar panel">
        <div class="brand">
          <div class="brand-mark"></div>
          <div>
            <h1 class="brand-title">CBCL</h1>
            <p class="brand-copy">Polymarket Bot Terminal</p>
          </div>
        </div>
        <nav class="nav nav-list">${renderNav()}</nav>
        <div class="sidebar-foot">
          ${renderSidebarStatus()}
        </div>
      </aside>

      <section class="workspace">
        ${renderTopbar()}
        ${renderSummaryStrip()}
        ${renderMonitorStrip()}
        <main class="page-stack">${renderPage()}</main>
      </section>

      <aside class="rail">
        ${renderAlerts()}
        ${renderRuntimeRail()}
        ${renderLastEventRail("Last fill", execution.fills?.[0], "good")}
        ${renderLastEventRail("Last reject", execution.rejects?.[0], "warning")}
      </aside>
    </div>
  `;

  document.querySelectorAll("[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      state.page = button.getAttribute("data-page") || "overview";
      renderApp();
    });
  });

  $("#refresh-btn")?.addEventListener("click", fetchBootstrap);
  $("#theme-btn")?.addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    renderApp();
  });
}

document.addEventListener("visibilitychange", () => {
  scheduleRefreshLoop();
  void fetchBootstrap({ background: true });
});

void fetchBootstrap();
scheduleRefreshLoop();
