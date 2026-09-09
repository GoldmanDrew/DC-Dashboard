const test = require("node:test");
const assert = require("node:assert/strict");

require("../assets/price_basis.js");

require("../assets/realized_decay.js");
const RD = require("../assets/realized_decay.js");

const {
  buildDailyLogDragSeries,
  computeHorizonPeriodReturns,
  buildRollingPeriodReturnSeries,
  logToSimplePeriod,
  periodBorrowLog,
  periodBorrowLogCalendar,
  buildTradingCalendar,
  prepareDecayTrRows,
  latestContiguousRows,
  etfTrPrice,
  cumSplitFactor,
  filterSplitsNeedingCloseBasisFix,
  summarizeTrCoverage,
  collapsePartialHorizons,
} = RD;

function makeFlatSeries(n, etfDrift = 0, undDrift = 0) {
  const rows = [];
  let ep = 100;
  let up = 50;
  const t0 = Date.parse("2024-01-02T12:00:00Z");
  for (let i = 0; i < n; i += 1) {
    const ds = new Date(t0 + i * 86400000).toISOString().slice(0, 10);
    rows.push({
      date: ds,
      close_price: ep,
      etf_adj_close: ep,
      underlying_adj_close: up,
    });
    ep *= 1 + etfDrift;
    up *= 1 + undDrift;
  }
  return rows;
}

/** Sessions only — a stand-in NYSE calendar with no weekend rows. */
function makeWeekdaySeries(n, etfDrift = 0, undDrift = 0) {
  const rows = [];
  let ep = 100;
  let up = 50;
  const cursor = new Date(Date.parse("2026-01-05T12:00:00Z")); // a Monday
  for (let i = 0; i < n; i += 1) {
    while (cursor.getUTCDay() === 0 || cursor.getUTCDay() === 6) {
      cursor.setUTCDate(cursor.getUTCDate() + 1);
    }
    rows.push({
      date: cursor.toISOString().slice(0, 10),
      close_price: ep,
      etf_adj_close: ep,
      underlying_adj_close: up,
    });
    ep *= 1 + etfDrift;
    up *= 1 + undDrift;
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return rows;
}

test("flat prices → zero period return", () => {
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(makeFlatSeries(10), []), 2);
  const h = computeHorizonPeriodReturns(daily, [5], 0.05);
  assert.ok(Math.abs(h.horizons[0].grossLog) < 1e-10);
  assert.ok(Math.abs(h.horizons[0].grossSimple) < 1e-10);
});

test("net subtracts borrow over the period, billed on days held", () => {
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(makeFlatSeries(20, -0.005, 0), []), 2);
  const borrow = 0.252;
  const h = computeHorizonPeriodReturns(daily, [20], borrow);
  const row = h.horizons[0];
  assert.ok(row.grossLog > 0);
  assert.equal(row.borrowBasis, "calendar_act_360");
  assert.equal(row.borrowDays, row.windowCalendarDays);
  assert.ok(Math.abs(row.borrowLog - periodBorrowLogCalendar(borrow, row.windowCalendarDays)) < 1e-12);
  assert.ok(Math.abs(row.netLog - (row.grossLog - row.borrowLog)) < 1e-12);
  assert.ok(row.netSimple < row.grossSimple);
});

// A window holding 20 prints across 113 calendar days was billing 20 days of
// carry. Act/360 on days held is ~5.7x that, and the gap grows with the window.
test("sparse window bills every calendar day held, not one per print", () => {
  const sessions = makeWeekdaySeries(60);
  const calendar = buildTradingCalendar(sessions.map((r) => r.date));
  const sparseRows = sessions.filter((_, i) => i % 3 === 0);
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(sparseRows, []), 2);
  const borrow = 0.36;
  const row = computeHorizonPeriodReturns(daily, [20], borrow, { calendar }).horizons[0];

  assert.ok(row.obs < 20, `expected sparse prints, got ${row.obs}`);
  assert.ok(Math.abs(row.borrowLog - (borrow * row.windowCalendarDays) / 360) < 1e-12);
  // Strictly more carry than the old per-observation charge would have taken.
  assert.ok(row.borrowLog > periodBorrowLog(borrow, row.obs));
});

test("period gross equals endpoint log drag", () => {
  const rows = makeFlatSeries(25, -0.01, 0.002);
  const beta = 2;
  const tr = prepareDecayTrRows(rows, []);
  const daily = buildDailyLogDragSeries(tr, beta);
  const h = computeHorizonPeriodReturns(daily, [20], 0);
  const row = h.horizons[0];
  const start = tr[tr.length - 21];
  const end = tr[tr.length - 1];
  const endpoint = beta * Math.log(end.trUndPx / start.trUndPx)
    - Math.log(end.trEtfPx / start.trEtfPx);
  assert.ok(Math.abs(row.grossLog - endpoint) < 1e-9);
  assert.ok(Math.abs(row.etfStartPx - start.trEtfPx) < 1e-9);
  assert.ok(Math.abs(row.etfEndPx - end.trEtfPx) < 1e-9);
});

// "20 trading days" must mean the last 20 sessions, not the last 20 rows we hold.
// Direxion names print ~2x a week, so the row-count slice returned a window
// spanning 113 calendar days under a 20-day label.
test("horizon window is anchored to sessions, not to row count", () => {
  const sessions = makeWeekdaySeries(60);
  const calendar = buildTradingCalendar(sessions.map((r) => r.date));
  const sparseRows = sessions.filter((_, i) => i % 3 === 0);
  const tr = prepareDecayTrRows(sparseRows, []);
  const daily = buildDailyLogDragSeries(tr, 2);

  const anchored = computeHorizonPeriodReturns(daily, [20], 0, { calendar }).horizons[0];
  const legacy = computeHorizonPeriodReturns(daily, [20], 0).horizons[0];

  assert.equal(anchored.windowMode, "trading_date");
  assert.equal(legacy.windowMode, "observation_count");
  // The anchored window spans what its label claims; the row-count one runs ~3x long.
  assert.ok(Math.abs(anchored.windowCalendarDays - anchored.nominalCalendarDays) <= 5,
    `${anchored.windowCalendarDays}d vs nominal ${anchored.nominalCalendarDays}d`);
  assert.ok(legacy.windowCalendarDays > anchored.windowCalendarDays * 2);

  // Measured from far fewer prints than "20 days" implies — flagged, not hidden.
  assert.equal(anchored.sparse, true);
  assert.ok(anchored.coveragePct < 0.8);
  assert.equal(anchored.sufficient, true);

  // Sparse sampling does not bias the sum: it still telescopes to the endpoints.
  const start = tr.find((x) => x.date === anchored.startDate);
  const end = tr.find((x) => x.date === anchored.endDate);
  const endpoint = 2 * Math.log(end.trUndPx / start.trUndPx)
    - Math.log(end.trEtfPx / start.trEtfPx);
  assert.ok(Math.abs(anchored.grossLog - endpoint) < 1e-9);
});

test("dense series is not flagged sparse and keeps its window", () => {
  const sessions = makeWeekdaySeries(60);
  const calendar = buildTradingCalendar(sessions.map((r) => r.date));
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(sessions, []), 2);
  const row = computeHorizonPeriodReturns(daily, [20], 0, { calendar }).horizons[0];
  assert.equal(row.obs, 20);
  assert.equal(row.sparse, false);
  assert.equal(row.sufficient, true);
  assert.ok(Math.abs(row.stretchRatio - 1) < 0.001);
});

test("window that cannot reach its anchor is not sufficient", () => {
  const sessions = makeWeekdaySeries(60);
  const calendar = buildTradingCalendar(sessions.map((r) => r.date));
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(sessions.slice(-6), []), 2);
  const row = computeHorizonPeriodReturns(daily, [20], 0, { calendar }).horizons[0];
  assert.equal(row.sufficient, false);
  assert.ok(row.obs < 20);
});

// A twice-weekly printer never had 60 consecutive rows, so the rolling chart
// simply said "no data". Anchored windows plot it with fewer prints inside.
test("rolling series plots a sparse symbol when anchored to sessions", () => {
  const sessions = makeWeekdaySeries(60);
  const calendar = buildTradingCalendar(sessions.map((r) => r.date));
  const daily = buildDailyLogDragSeries(
    prepareDecayTrRows(sessions.filter((_, i) => i % 3 === 0), []),
    2,
  );
  assert.equal(buildRollingPeriodReturnSeries(daily, 20, 0.1).length, 0);
  const anchored = buildRollingPeriodReturnSeries(daily, 20, 0.1, { calendar });
  assert.ok(anchored.length > 0);
  anchored.forEach((r) => {
    assert.ok(Number.isFinite(r.gross_period));
    assert.ok(r.obs > 0 && r.obs < 20);
    assert.ok(r.windowCalendarDays >= 20);
  });
});

test("collapsePartialHorizons dedupes identical longer partials", () => {
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(makeFlatSeries(26, -0.002, 0.001), []), -2);
  const raw = computeHorizonPeriodReturns(daily, [5, 20, 60, 120, 251], 0.1);
  const collapsed = collapsePartialHorizons(raw);
  const partials = collapsed.horizons.filter((h) => !h.sufficient);
  assert.equal(partials.length, 1);
  assert.equal(partials[0].availableHistory, true);
  assert.ok(collapsed.horizons.filter((h) => h.sufficient).length >= 1);
});

test("rolling period series length", () => {
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(makeFlatSeries(40, -0.002, 0.001), []), 2);
  const roll = buildRollingPeriodReturnSeries(daily, 20, 0.1);
  assert.equal(roll.length, daily.length - 19);
  roll.forEach((r) => {
    assert.ok(Number.isFinite(r.gross_period));
    assert.ok(Number.isFinite(r.net_period));
  });
});

test("latest contiguous rows cut ticker-reuse lifecycle gap", () => {
  const oldRows = makeFlatSeries(65, 0, 0.001).map((r) => ({
    ...r,
    source_provider: "yahoo_bootstrap",
  }));
  const newRows = [2, 3, 4, 5, 8].map((day, i) => ({
    date: `2026-06-${String(day).padStart(2, "0")}`,
    close_price: 17 + i,
    etf_adj_close: 17 + i,
    underlying_adj_close: 128 + i,
    source_provider: "merged",
  }));
  const seg = latestContiguousRows(oldRows.concat(newRows));
  assert.deepEqual(seg.map((r) => r.date), newRows.map((r) => r.date));
});

test("decay horizons do not bridge ticker-reuse lifecycle gap", () => {
  const oldRows = makeFlatSeries(70, 0, 0.002);
  const newRows = [2, 3, 4, 5, 8].map((day, i) => ({
    date: `2026-06-${String(day).padStart(2, "0")}`,
    close_price: 17 + i,
    etf_adj_close: 17 + i,
    underlying_adj_close: 128 + i,
  }));
  const daily = buildDailyLogDragSeries(prepareDecayTrRows(oldRows.concat(newRows), []), 2);
  const h = computeHorizonPeriodReturns(daily, [60], 0.1);
  const row = h.horizons[0];
  assert.equal(row.obs, 4);
  assert.equal(row.sufficient, false);
});

test("logToSimplePeriod", () => {
  assert.ok(Math.abs(logToSimplePeriod(0.1) - 0.105170918) < 1e-6);
});

test("prefers etf_adj_close over raw close for TR drag", () => {
  const rows = [
    { date: "2024-01-01", close_price: 100, etf_adj_close: 100, underlying_adj_close: 50 },
    { date: "2024-01-02", close_price: 50, etf_adj_close: 99, underlying_adj_close: 50 },
  ];
  const tr = prepareDecayTrRows(rows, []);
  const daily = buildDailyLogDragSeries(tr, 2);
  assert.equal(daily.length, 1);
  assert.ok(Math.abs(daily[0].drag - (2 * Math.log(50 / 50) - Math.log(99 / 100))) < 1e-9);
});

test("cumSplitFactor scales pre-split close to latest basis", () => {
  const events = [{ date: "2024-01-02", mult: 6 }];
  assert.ok(Math.abs(cumSplitFactor("2024-01-01", "2024-01-02", events) - 6) < 1e-9);
  const tr = prepareDecayTrRows([
    { date: "2024-01-01", close_price: 4, underlying_adj_close: 1 },
    { date: "2024-01-02", close_price: 24, underlying_adj_close: 1 },
  ], events);
  assert.ok(Math.abs(tr[0].trEtfPx - 24) < 1e-9);
});

test("filter skips continuous Yahoo MTYY reverse split", () => {
  const points = [
    { date: "2026-05-28", close: 24.0 },
    { date: "2026-06-01", close: 23.604 },
    { date: "2026-06-02", close: 22.99 },
  ];
  const events = [{ date: "2026-06-02", mult: 6 }];
  assert.deepEqual(filterSplitsNeedingCloseBasisFix(points, events), []);
});

test("MTYY issuer pre-split to post-split: decay gross not ~-80%", () => {
  const rows = [];
  for (let i = 0; i < 55; i += 1) {
    const day = String(10 + (i % 20)).padStart(2, "0");
    rows.push({
      date: `2026-04-${day}`,
      close_price: 4.3 - i * 0.002,
      etf_adj_close: 4.2 - i * 0.002,
      nav_total_return: 4.35 - i * 0.002,
      underlying_adj_close: 170 - i * 0.1,
    });
  }
  rows.push(
    { date: "2026-05-28", close_price: 4.0, etf_adj_close: null, nav_total_return: 4.12, underlying_adj_close: 151.64 },
    { date: "2026-06-01", close_price: 23.604, etf_adj_close: 23.604, nav_total_return: 23.65, underlying_adj_close: 136.08 },
    { date: "2026-06-02", close_price: 22.99, etf_adj_close: 22.99, nav_total_return: 23.02, underlying_adj_close: 136.08 },
  );
  const events = [{ date: "2026-06-02", mult: 6 }];
  const tr = prepareDecayTrRows(rows, events);
  const daily = buildDailyLogDragSeries(tr, 0.5);
  const h = computeHorizonPeriodReturns(daily, [20, 60], 0.09);
  const row20 = h.horizons.find((x) => x.horizonDays === 20);
  assert.ok(row20.grossSimple > -0.35, `20d gross too negative: ${row20.grossSimple}`);
  assert.ok(row20.etfEndPx / row20.etfStartPx > 0.75 && row20.etfEndPx / row20.etfStartPx < 1.25,
    `etf px ratio ${row20.etfStartPx} -> ${row20.etfEndPx}`);
});

test("distribution reinvestment scales with verified split", () => {
  const rows = [
    { date: "2026-05-15", close_price: 4.4, etf_adj_close: 4.33, nav_total_return: 4.55, underlying_adj_close: 177.0 },
    { date: "2026-05-28", close_price: 4.0, etf_adj_close: null, nav_total_return: 4.12, underlying_adj_close: 151.64 },
    { date: "2026-06-02", close_price: 22.99, etf_adj_close: 22.99, nav_total_return: 23.02, underlying_adj_close: 136.08 },
  ];
  const tr = prepareDecayTrRows(rows, [{ date: "2026-06-02", mult: 6 }]);
  const may15 = tr.find((x) => x.date === "2026-05-15");
  const may28 = tr.find((x) => x.date === "2026-05-28");
  assert.ok(may15.trEtfPx > 24 && may15.trEtfPx < 28, `May15 TR px ${may15.trEtfPx}`);
  assert.ok(may28.trEtfPx > 23 && may28.trEtfPx < 27, `May28 TR px ${may28.trEtfPx}`);
  assert.ok(Math.abs(may15.trEtfPx - 4.33 * 6) < 0.5);
});

test("no double-scale when navTr already inflated vs close", () => {
  const rows = [
    { date: "2026-05-20", close_price: 4.2, nav_total_return: 250, underlying_adj_close: 160 },
    { date: "2026-06-02", close_price: 23, etf_adj_close: 23, nav_total_return: 23, underlying_adj_close: 136 },
  ];
  const tr = prepareDecayTrRows(rows, [{ date: "2026-06-02", mult: 6 }]);
  const pre = tr.find((x) => x.date === "2026-05-20");
  assert.ok(pre.trEtfPx < 30, `double-scale leak ${pre.trEtfPx}`);
});

test("pre-split adj mapped consistently across close threshold", () => {
  const rows = [
    { date: "2025-11-14", close_price: 12.65, etf_adj_close: 7.05, underlying_adj_close: 200 },
    { date: "2025-11-17", close_price: 12.01, etf_adj_close: 6.69, underlying_adj_close: 195 },
    { date: "2026-06-01", close_price: 3.934, etf_adj_close: 3.934, underlying_adj_close: 150 },
    { date: "2026-06-02", close_price: 22.99, etf_adj_close: 22.99, underlying_adj_close: 136 },
  ];
  const tr = prepareDecayTrRows(rows, [{ date: "2026-06-02", mult: 6 }]);
  const nov14 = tr.find((x) => x.date === "2025-11-14");
  const nov17 = tr.find((x) => x.date === "2025-11-17");
  assert.ok(Math.abs(nov14.trEtfPx - 7.05 * 6) < 0.05);
  assert.ok(Math.abs(nov17.trEtfPx - 6.69 * 6) < 0.05);
  const novRet = Math.log(nov17.trEtfPx / nov14.trEtfPx);
  assert.ok(Math.abs(novRet) < 0.15, `Nov cliff ${novRet}`);
});

test("MTYY staggered adj-before-close: no TR cliff on adj switch", () => {
  const rows = [
    { date: "2026-05-15", close_price: 4.401, etf_adj_close: 4.334, underlying_adj_close: 170 },
    { date: "2026-05-20", close_price: 4.26, etf_adj_close: 4.196, underlying_adj_close: 165 },
    { date: "2026-05-21", close_price: 4.23, etf_adj_close: 4.166, underlying_adj_close: 164.85 },
    { date: "2026-05-27", close_price: 4.04, etf_adj_close: 0.673, underlying_adj_close: 154.2 },
    { date: "2026-05-28", close_price: 4.0, etf_adj_close: 0.667, underlying_adj_close: 151.64 },
    { date: "2026-06-01", close_price: 3.934, etf_adj_close: 0.656, underlying_adj_close: 149.78 },
    { date: "2026-06-02", close_price: 22.99, etf_adj_close: 3.832, underlying_adj_close: 136.08 },
    { date: "2026-06-08", close_price: 22.515, etf_adj_close: 22.515, underlying_adj_close: 130 },
  ];
  const events = [{ date: "2026-06-02", mult: 6 }];
  const tr = prepareDecayTrRows(rows, events);
  const may21 = tr.find((x) => x.date === "2026-05-21");
  const may27 = tr.find((x) => x.date === "2026-05-27");
  const jun02 = tr.find((x) => x.date === "2026-06-02");
  assert.ok(may21.trEtfPx > 24 && may21.trEtfPx < 26, `May21 TR ${may21.trEtfPx}`);
  assert.ok(may27.trEtfPx > 23 && may27.trEtfPx < 26, `May27 TR cliff ${may21.trEtfPx} -> ${may27.trEtfPx}`);
  assert.ok(Math.abs(may27.trEtfPx / may21.trEtfPx - 1) < 0.08, "TR should be continuous across adj switch");
  assert.ok(jun02.trEtfPx > 22 && jun02.trEtfPx < 24, `Jun02 TR ${jun02.trEtfPx}`);
  const daily = buildDailyLogDragSeries(tr, 0.5);
  const h = computeHorizonPeriodReturns(daily, [5, 20], 0.09);
  const row5 = h.horizons.find((x) => x.horizonDays === 5);
  assert.ok(row5.grossSimple > -0.35, `5d gross too negative: ${row5.grossSimple}`);
  const cov = summarizeTrCoverage(rows, events);
  assert.equal(cov.splitMode, "staggered_reverse_adj_first");
  assert.ok(cov.maxEtfDailyLogReturn < 0.35, `max jump ${cov.maxEtfDailyLogReturn}`);
});

test("summarizeTrCoverage reports split mode and joint days", () => {
  const rows = [
    { date: "2026-05-28", close_price: 4.0, etf_adj_close: 3.94, underlying_adj_close: 150 },
    { date: "2026-06-02", close_price: 22.99, etf_adj_close: 22.99, underlying_adj_close: 136 },
  ];
  const cov = summarizeTrCoverage(rows, [{ date: "2026-06-02", mult: 6 }]);
  assert.ok(cov);
  assert.equal(cov.trJointDays, 2);
  assert.equal(cov.splitMode, "discrete_split");
  assert.ok(cov.primaryEtfBasis.includes("yahoo") || cov.primaryEtfBasis === "split_adjusted");
});

test("summarizeTrCoverage degrades quality when ETF adj is missing", () => {
  const rows = [];
  for (let i = 0; i < 25; i += 1) {
    rows.push({
      date: `2026-06-${String(i + 1).padStart(2, "0")}`,
      close_price: 25 - i * 0.05,
      nav: 25 - i * 0.05,
      underlying_adj_close: 100 + i * 0.1,
    });
  }
  const cov = summarizeTrCoverage(rows, []);
  assert.equal(cov.etfAdjDays, 0);
  assert.equal(cov.quality, "degraded");
  assert.ok((cov.warnings || []).some((w) => /ETF Yahoo adj close missing/.test(w)));
});
