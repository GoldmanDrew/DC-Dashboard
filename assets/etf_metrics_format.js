/**
 * Reader for data/etf_metrics_daily.json.
 *
 * The published file is columnar_v1: field names once, one array per field.
 * expand() turns that back into the row objects the rest of the dashboard
 * already reads (close_price, etf_adj_close, stale_kind, ...).
 *
 * Writer contract, including which provenance fields are dropped and when
 * etf_adj_close is stored as null: scripts/etf_metrics_format.py.
 *
 * null etf_adj_close means "same as close_price", unless the row index is
 * listed in etf_adj_close_missing (adjusted close genuinely absent). A stored
 * number always wins, including when it differs from the close.
 */
(function initEtfMetricsFormat(globalObj) {
  const FORMAT = "columnar_v1";
  const COLUMN_KEYS = [
    "ticker_ix",
    "date",
    "close_price",
    "nav",
    "etf_adj_close",
    "underlying_adj_close",
    "aum",
    "shares_outstanding",
    "shares_traded",
    "status",
    "stale",
    "stale_kind",
    "stale_age_bdays",
    "issuer_asof_date",
    "market_asof_date",
    "premium_discount_eligible",
    "premium_discount_status",
  ];

  function expand(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("etf metrics payload must be a JSON object");
    }
    if (payload.format !== FORMAT && Array.isArray(payload.rows)) {
      return payload.rows.filter((row) => row && typeof row === "object");
    }
    if (payload.format !== FORMAT) {
      throw new Error("etf metrics must be columnar_v1 or a legacy rows array");
    }
    return expandColumnar(payload);
  }

  function expandColumnar(payload) {
    const columns = payload.columns;
    if (!columns || typeof columns !== "object" || Array.isArray(columns)) {
      throw new Error("columnar_v1 metrics missing columns");
    }
    const tickers = payload.tickers;
    if (!Array.isArray(tickers) || tickers.some((ticker) => typeof ticker !== "string" || !ticker)) {
      throw new Error("columnar_v1 tickers must be a list of strings");
    }

    const arrays = {};
    let n = null;
    for (const key of COLUMN_KEYS) {
      const values = columns[key];
      if (!Array.isArray(values)) {
        throw new Error(`columnar_v1 column ${key} must be an array`);
      }
      if (n === null) n = values.length;
      else if (values.length !== n) {
        throw new Error(`columnar_v1 column ${key} has length ${values.length}, expected ${n}`);
      }
      arrays[key] = values;
    }

    const missing = new Set();
    const missingRaw = Array.isArray(payload.etf_adj_close_missing) ? payload.etf_adj_close_missing : [];
    if (payload.etf_adj_close_missing != null && !Array.isArray(payload.etf_adj_close_missing)) {
      throw new Error("etf_adj_close_missing must be an array of indexes");
    }
    for (const index of missingRaw) {
      if (!Number.isInteger(index) || index < 0 || index >= n) {
        throw new Error(`etf_adj_close_missing index out of range: ${index}`);
      }
      missing.add(index);
    }

    const rows = new Array(n);
    for (let i = 0; i < n; i += 1) {
      const tickerIx = arrays.ticker_ix[i];
      if (!Number.isInteger(tickerIx) || tickerIx < 0 || tickerIx >= tickers.length) {
        throw new Error(`ticker_ix out of range at row ${i}: ${tickerIx}`);
      }
      const close = arrays.close_price[i];
      let adj = arrays.etf_adj_close[i];
      if (adj == null && close != null && !missing.has(i)) adj = close;
      rows[i] = {
        date: arrays.date[i],
        ticker: tickers[tickerIx],
        nav: arrays.nav[i],
        aum: arrays.aum[i],
        shares_outstanding: arrays.shares_outstanding[i],
        shares_traded: arrays.shares_traded[i],
        status: arrays.status[i],
        stale: arrays.stale[i],
        stale_age_bdays: arrays.stale_age_bdays[i],
        stale_kind: arrays.stale_kind[i],
        close_price: close,
        etf_adj_close: adj,
        underlying_adj_close: arrays.underlying_adj_close[i],
        issuer_asof_date: arrays.issuer_asof_date[i],
        market_asof_date: arrays.market_asof_date[i],
        premium_discount_eligible: arrays.premium_discount_eligible[i],
        premium_discount_status: arrays.premium_discount_status[i],
      };
    }
    return rows;
  }

  const exported = {
    FORMAT,
    COLUMN_KEYS,
    expand,
    expandColumnar,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = exported;
  }
  if (globalObj) {
    globalObj.EtfMetricsFormat = exported;
  }
})(typeof window !== "undefined" ? window : globalThis);
