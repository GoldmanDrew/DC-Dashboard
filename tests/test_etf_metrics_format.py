"""Round-trip and published-file checks for columnar ETF metrics."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import etf_metrics_format as emf  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
METRICS = ROOT / "data" / "etf_metrics_daily.json"
MAX_DEPLOYED = 20 * 1024 * 1024


def _row(**overrides):
    base = {
        "date": "2026-06-17",
        "ticker": "aaa",
        "nav": 10.44444,
        "aum": 1000.5,
        "shares_outstanding": 100.2,
        "shares_traded": 5.5,
        "source_provider": "yfinance",
        "source_url": "https://example.invalid/aaa",
        "ingested_at_utc": "2026-06-18 00:00:00+00:00",
        "status": "ok",
        "stale": False,
        "stale_age_bdays": 0.0,
        "close_price": 10.5,
        "underlying_adj_close": 20.00004,
        "etf_adj_close": 10.5,
        "stale_kind": None,
        "issuer_asof_date": "2026-06-17",
        "market_asof_date": "2026-06-17",
        "premium_discount_eligible": True,
        "premium_discount_status": "valid",
    }
    base.update(overrides)
    return base


class RoundingTests(unittest.TestCase):
    def test_prices_round_half_up_to_4dp(self):
        self.assertEqual(emf.round_price(1.23445), 1.2345)
        self.assertEqual(emf.round_price(1.23444), 1.2344)
        self.assertEqual(emf.round_price(15.116000175476074), 15.116)
        self.assertIsNone(emf.round_price(None))

    def test_integers_round_half_up(self):
        self.assertEqual(emf.round_int(1.5), 2)
        self.assertEqual(emf.round_int(2.5), 3)
        self.assertEqual(emf.round_int(1000.4), 1000)
        self.assertIsNone(emf.round_int(None))


class CompactTests(unittest.TestCase):
    def _payload(self):
        return {
            "build_time": "2026-06-18T00:00:00Z",
            "public_history_days": 120,
            "rows": [
                _row(),
                _row(
                    date="2026-06-18",
                    ticker="BBB",
                    close_price=11,
                    etf_adj_close=9.5,
                    nav=11.1,
                    aum=None,
                    shares_outstanding=None,
                    shares_traded=None,
                    stale=True,
                    stale_kind="carry_forward",
                    stale_age_bdays=2,
                    premium_discount_eligible=False,
                    premium_discount_status="issuer_stale",
                ),
                _row(
                    date="2026-06-17",
                    ticker="BBB",
                    close_price=9,
                    etf_adj_close=None,
                    nav=None,
                    status="missing",
                ),
            ],
        }

    def test_drops_provenance_and_equal_adj_close(self):
        compact = emf.compact(self._payload())
        blob = emf.dumps(compact)
        for key in emf.PROVENANCE_KEYS:
            self.assertNotIn(f'"{key}"', blob)
        self.assertEqual(compact["format"], "columnar_v1")
        self.assertEqual(compact["tickers"], ["AAA", "BBB"])
        # Row 0 adj equals close -> stored null. Row 1 differs. Row 2 truly missing.
        self.assertEqual(compact["columns"]["etf_adj_close"], [None, 9.5, None])
        self.assertEqual(compact["etf_adj_close_missing"], [2])
        self.assertEqual(compact["columns"]["aum"], [1001, None, 1001])
        self.assertEqual(compact["columns"]["shares_traded"], [6, None, 6])
        self.assertEqual(compact["columns"]["underlying_adj_close"][0], 20.0)

    def test_expand_restores_equal_adj_and_keeps_real_gaps(self):
        rows = emf.expand(emf.compact(self._payload()))
        self.assertEqual(rows[0]["ticker"], "AAA")
        self.assertEqual(rows[0]["etf_adj_close"], rows[0]["close_price"])
        self.assertEqual(rows[1]["etf_adj_close"], 9.5)
        self.assertEqual(rows[1]["close_price"], 11)
        self.assertIsNone(rows[2]["etf_adj_close"])
        self.assertEqual(rows[2]["close_price"], 9)
        self.assertEqual(rows[1]["stale_kind"], "carry_forward")
        for row in rows:
            for key in emf.PROVENANCE_KEYS:
                self.assertNotIn(key, row)

    def test_compact_is_stable(self):
        once = emf.compact(self._payload())
        twice = emf.compact(once)
        self.assertEqual(emf.dumps(once), emf.dumps(twice))

    def test_legacy_rows_still_expand(self):
        payload = self._payload()
        self.assertEqual(len(emf.expand(payload)), 3)
        self.assertEqual(emf.expand(payload)[0]["source_provider"], "yfinance")


class PublishedFileTests(unittest.TestCase):
    def test_published_file_is_columnar_and_under_the_deploy_cap(self):
        raw = METRICS.read_text(encoding="utf-8")
        self.assertLess(len(raw.encode("utf-8")), MAX_DEPLOYED)
        payload = json.loads(raw)
        self.assertEqual(payload.get("format"), "columnar_v1")
        for key in emf.PROVENANCE_KEYS:
            self.assertNotIn(key, raw)
        rows = emf.expand_columnar(payload)
        self.assertGreater(len(rows), 1000)
        columns = payload["columns"]
        missing = set(payload.get("etf_adj_close_missing") or [])
        for i, row in enumerate(rows):
            self.assertTrue(row["ticker"])
            self.assertEqual(row["date"], columns["date"][i])
            stored = columns["etf_adj_close"][i]
            close = columns["close_price"][i]
            if stored is not None:
                self.assertEqual(row["etf_adj_close"], stored)
            elif i in missing:
                self.assertIsNone(row["etf_adj_close"])
            else:
                self.assertEqual(row["etf_adj_close"], close)
            for key in ("close_price", "nav", "underlying_adj_close"):
                value = columns[key][i]
                if isinstance(value, float):
                    text = json.dumps(value)
                    places = len(text.split(".")[1]) if "." in text else 0
                    self.assertLessEqual(places, 4, f"{key}[{i}]={text}")
            for key in ("aum", "shares_outstanding", "shares_traded", "stale_age_bdays"):
                value = columns[key][i]
                self.assertTrue(value is None or isinstance(value, int), f"{key}[{i}]={value!r}")


if __name__ == "__main__":
    unittest.main()
