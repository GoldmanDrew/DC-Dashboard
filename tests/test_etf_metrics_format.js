const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const EMF = require("../assets/etf_metrics_format.js");
require("../assets/realized_decay.js");
const RD = require("../assets/realized_decay.js");

function columns(n, fill) {
  const out = {};
  for (const key of EMF.COLUMN_KEYS) out[key] = Array.from({ length: n }, (_, i) => fill(key, i));
  return out;
}

test("null adjusted close copies close unless the row is genuinely missing", () => {
  const payload = {
    format: "columnar_v1",
    build_time: "2026-06-18T00:00:00Z",
    public_history_days: 120,
    tickers: ["AAA", "BBB"],
    etf_adj_close_missing: [2],
    columns: columns(3, (key, i) => {
      const table = {
        ticker_ix: [0, 1, 1],
        date: ["2026-06-17", "2026-06-18", "2026-06-17"],
        close_price: [10.5, 11, 9],
        nav: [10.4, 11.1, null],
        etf_adj_close: [null, 9.5, null],
        underlying_adj_close: [20, 21, 22],
        aum: [1001, null, 50],
        shares_outstanding: [100, 100, null],
        shares_traded: [6, 0, null],
        status: ["ok", "ok", "missing"],
        stale: [false, true, false],
        stale_kind: [null, "carry_forward", null],
        stale_age_bdays: [null, 2, null],
        issuer_asof_date: ["2026-06-17", "2026-06-18", "2026-06-17"],
        market_asof_date: ["2026-06-17", "2026-06-18", null],
        premium_discount_eligible: [true, false, false],
        premium_discount_status: ["valid", "issuer_stale", "provider_missing"],
      };
      return table[key][i];
    }),
  };
  const rows = EMF.expand(payload);
  assert.equal(rows[0].ticker, "AAA");
  assert.equal(rows[0].etf_adj_close, 10.5);
  assert.equal(rows[1].ticker, "BBB");
  assert.equal(rows[1].etf_adj_close, 9.5);
  assert.equal(rows[2].etf_adj_close, null);
  assert.equal(rows[2].close_price, 9);
  assert.equal(rows[1].shares_traded, 0);
  assert.equal(rows[2].shares_traded, null);
  for (const row of rows) {
    assert.equal(row.source_url, undefined);
    assert.equal(row.source_provider, undefined);
    assert.equal(row.ingested_at_utc, undefined);
  }
});

test("legacy rows pass through with their own fields", () => {
  const rows = [{ date: "2026-06-17", ticker: "AAA", source_provider: "merged" }];
  assert.deepEqual(EMF.expand({ rows }), rows);
});

test("carry_forward stale_kind alone is not a usable price", () => {
  // Provenance URLs are not on the public file. Carry-forward still has to
  // drop out of the total-return series via stale_kind.
  assert.equal(RD.hasUsableMetricPrices({
    date: "2026-06-02",
    close_price: 10,
    underlying_adj_close: 12,
    stale_kind: "carry_forward",
  }), false);
  assert.equal(RD.hasUsableMetricPrices({
    date: "2026-06-02",
    close_price: 10,
    underlying_adj_close: 12,
    etf_adj_close: 10,
    stale_kind: null,
  }), true);
});

test("published metrics file expands and restores omitted adjusted closes", () => {
  const file = path.join(__dirname, "..", "data", "etf_metrics_daily.json");
  const payload = JSON.parse(fs.readFileSync(file, "utf8"));
  assert.equal(payload.format, "columnar_v1");
  const rows = EMF.expand(payload);
  assert.ok(rows.length > 10000);
  const missing = new Set(payload.etf_adj_close_missing || []);
  const cols = payload.columns;
  assert.equal(rows.length, cols.date.length);
  for (let i = 0; i < rows.length; i += 1) {
    const stored = cols.etf_adj_close[i];
    if (stored != null) assert.equal(rows[i].etf_adj_close, stored);
    else if (missing.has(i)) assert.equal(rows[i].etf_adj_close, null);
    else assert.equal(rows[i].etf_adj_close, cols.close_price[i]);
    assert.equal(rows[i].ticker, payload.tickers[cols.ticker_ix[i]]);
  }
});
