"""Tests for scripts/public_health_sentinel.py (stdlib unittest — no pip deps).

`requirements.txt` is in validate_public_boundary.FORBIDDEN_FILES, so this
repository cannot declare a Python dependency. Run with:

    python -m unittest discover -s tests -p "test_*.py" -t .

The Node suite (`node --test tests/`) ignores .py files, and validate.yml's
`for test_file in tests/*.js` loop does too, so the two suites coexist.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import public_health_sentinel as phs  # noqa: E402


class CalendarTests(unittest.TestCase):
    def test_known_2026_holidays_are_not_sessions(self):
        for d, name in [
            (date(2026, 1, 1), "New Year's Day"),
            (date(2026, 1, 19), "MLK"),
            (date(2026, 2, 16), "Washington's Birthday"),
            (date(2026, 4, 3), "Good Friday"),
            (date(2026, 5, 25), "Memorial Day"),
            (date(2026, 6, 19), "Juneteenth"),
            (date(2026, 7, 3), "Independence Day (observed)"),
            (date(2026, 9, 7), "Labor Day"),
            (date(2026, 11, 26), "Thanksgiving"),
            (date(2026, 12, 25), "Christmas"),
        ]:
            self.assertFalse(phs.is_session(d), f"{name} {d} should not be a session")

    def test_ordinary_weekday_is_a_session_and_weekend_is_not(self):
        self.assertTrue(phs.is_session(date(2026, 8, 10)))   # Monday
        self.assertFalse(phs.is_session(date(2026, 8, 8)))   # Saturday
        self.assertFalse(phs.is_session(date(2026, 8, 9)))   # Sunday

    def test_market_age_excludes_whole_non_session_days(self):
        friday = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)
        monday = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
        raw = (monday - friday).total_seconds() / 3600.0
        aged = phs.market_age_hours(friday, monday)
        self.assertAlmostEqual(raw - 48.0, aged, places=6)
        self.assertLess(aged, 24.0)

    def test_future_timestamp_is_zero_age(self):
        now = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
        self.assertEqual(phs.market_age_hours(datetime(2026, 8, 11, 0, 0, tzinfo=UTC), now), 0.0)


class BrowserStrictParseTests(unittest.TestCase):
    def test_nan_and_infinity_rejected(self):
        tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        for token in ("NaN", "Infinity", "-Infinity"):
            p = tmp / "x.json"
            p.write_text('{"a": %s}' % token, encoding="utf-8")
            with self.assertRaises(ValueError, msg=f"{token} must be rejected"):
                phs.load_browser_strict(p)

    def test_ordinary_payload_parses(self):
        tmp = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        p = tmp / "x.json"
        p.write_text('{"a": 1.5, "b": null, "c": [1,2]}', encoding="utf-8")
        self.assertEqual(phs.load_browser_strict(p)["a"], 1.5)


class LivenessTests(unittest.TestCase):
    NOW = datetime(2026, 8, 10, 17, 0, tzinfo=UTC)  # Monday

    def _payloads(self, stamps: dict[str, str]) -> dict[str, object]:
        return {rel: {"build_time": ts} for rel, ts in stamps.items()}

    def test_fresh_bundle_has_no_findings(self):
        out = phs.check_liveness(
            self._payloads({"data/a.json": "2026-08-10T16:30:00Z",
                            "data/b.json": "2026-08-10T16:00:00Z"}), self.NOW)
        self.assertEqual(out, [])

    def test_stalled_publisher_fails(self):
        out = phs.check_liveness(
            self._payloads({"data/a.json": "2026-08-06T16:30:00Z"}), self.NOW)
        codes = [f["code"] for f in out]
        self.assertIn("bundle_not_publishing", codes)
        self.assertEqual(out[0]["severity"], phs.FAIL)

    def test_mildly_stale_warns_but_does_not_fail(self):
        out = phs.check_liveness(
            self._payloads({"data/a.json": "2026-08-10T08:00:00Z"}), self.NOW)
        self.assertEqual([f["code"] for f in out], ["bundle_stale"])
        self.assertEqual(out[0]["severity"], phs.WARN)

    def test_daily_cadence_artifact_does_not_trip_skew_over_a_weekend(self):
        # Regression guard: an artifact last built Saturday is ~29 wall-clock
        # hours behind a Monday bundle. Wall-clock skew fired here; market-hours
        # skew against a 48h budget must not.
        out = phs.check_liveness(
            self._payloads({"data/newest.json": "2026-08-10T17:00:00Z",
                            "data/daily.json": "2026-08-08T11:48:00Z"}), self.NOW)
        self.assertEqual([f["code"] for f in out], [])

    def test_frozen_artifact_trips_skew(self):
        out = phs.check_liveness(
            self._payloads({"data/newest.json": "2026-08-10T17:00:00Z",
                            "data/frozen.json": "2026-07-20T11:48:00Z"}), self.NOW)
        self.assertIn("bundle_skew", [f["code"] for f in out])

    def test_bundle_without_build_times_fails(self):
        out = phs.check_liveness({"data/a.json": {"rows": []}}, self.NOW)
        self.assertEqual(out[0]["code"], "no_build_times")


class VerdictTests(unittest.TestCase):
    def test_precedence(self):
        f = phs.finding(phs.FAIL, "c", "a", "d")
        w = phs.finding(phs.WARN, "c", "a", "d")
        self.assertEqual(phs.verdict_of([w, f]), phs.FAIL)
        self.assertEqual(phs.verdict_of([w]), phs.WARN)
        self.assertEqual(phs.verdict_of([]), "pass")

    def test_fingerprint_is_order_insensitive_and_content_sensitive(self):
        a = [phs.finding(phs.FAIL, "x", "data/a.json", "one"),
             phs.finding(phs.WARN, "y", "data/b.json", "two")]
        self.assertEqual(phs._fingerprint(a), phs._fingerprint(list(reversed(a))))
        self.assertNotEqual(
            phs._fingerprint(a),
            phs._fingerprint(a + [phs.finding(phs.WARN, "z", "data/c.json", "three")]))


class RecordCountTests(unittest.TestCase):
    def test_locator_resolution(self):
        self.assertEqual(phs.count_records({"records": [1, 2, 3]}, "records"), 3)
        self.assertEqual(phs.count_records({"by_symbol": {"A": 1}}, "by_symbol"), 1)
        self.assertIsNone(phs.count_records({"other": []}, "records"))
        self.assertIsNone(phs.count_records([], "records"))
        self.assertIsNone(phs.count_records({"records": 5}, "records"))


class ManifestCoverageTests(unittest.TestCase):
    def test_unlisted_file_is_reported(self):
        # Uses the real repository tree; every published file must be listed.
        payloads = {"data/public_bundle_manifest.json":
                    json.loads((phs.MANIFEST).read_text(encoding="utf-8"))}
        out = phs.check_manifest_coverage(payloads)
        self.assertEqual(out, [], f"live bundle has coverage findings: {out}")

    def test_missing_manifest_is_a_failure(self):
        out = phs.check_manifest_coverage({})
        self.assertEqual(out[0]["code"], "manifest_unreadable")


class EscalationTests(unittest.TestCase):
    """
    The watchdog's job is to be heard. It filed one warn on 2026-08-19 and then
    said nothing for three weeks while the bundle went from stale to three-weeks
    dead: the fingerprint stopped changing, so every later run took the
    "unchanged findings" path. These cover the paths that must still speak.
    """

    FAIL_FINDINGS = [{"severity": phs.FAIL, "code": "bundle_not_publishing",
                      "artifact": "data/", "detail": "dead", "stale_days": 23}]
    WARN_FINDINGS = [{"severity": phs.WARN, "code": "bundle_stale",
                      "artifact": "data/", "detail": "slow"}]

    def test_first_report_speaks(self):
        speak, _ = phs.should_speak(None, phs.FAIL, "abc", "2026-09-09")
        self.assertTrue(speak)

    def test_warn_to_fail_escalation_always_speaks(self):
        prev = {"fp": "abc", "verdict": phs.WARN, "day": "2026-09-09"}
        speak, why = phs.should_speak(prev, phs.FAIL, "abc", "2026-09-09")
        self.assertTrue(speak)
        self.assertIn("escalated", why)

    def test_unresolved_fail_nags_once_a_day(self):
        prev = {"fp": "abc", "verdict": phs.FAIL, "day": "2026-09-08"}
        speak, why = phs.should_speak(prev, phs.FAIL, "abc", "2026-09-09")
        self.assertTrue(speak, "a fail that is still failing must not go silent")
        self.assertEqual(why, "still failing")

    def test_same_day_fail_does_not_spam(self):
        prev = {"fp": "abc", "verdict": phs.FAIL, "day": "2026-09-09"}
        speak, _ = phs.should_speak(prev, phs.FAIL, "abc", "2026-09-09")
        self.assertFalse(speak)

    def test_steady_warn_stays_quiet(self):
        prev = {"fp": "abc", "verdict": phs.WARN, "day": "2026-09-08"}
        speak, _ = phs.should_speak(prev, phs.WARN, "abc", "2026-09-09")
        self.assertFalse(speak)

    def test_changed_findings_speak(self):
        prev = {"fp": "abc", "verdict": phs.WARN, "day": "2026-09-09"}
        speak, why = phs.should_speak(prev, phs.WARN, "def", "2026-09-09")
        self.assertTrue(speak)
        self.assertEqual(why, "findings changed")

    def test_title_carries_the_age(self):
        title = phs.alert_title(phs.FAIL, self.FAIL_FINDINGS)
        self.assertIn("23 days ago", title)
        self.assertIn(phs.FAIL, title)

    def test_title_falls_back_to_finding_count(self):
        self.assertIn("1 finding(s)", phs.alert_title(phs.WARN, self.WARN_FINDINGS))

    def test_stale_days_is_not_part_of_the_fingerprint(self):
        # Only (code, artifact, severity) identify a finding; a growing day count
        # must not masquerade as "findings changed".
        day1 = [dict(self.FAIL_FINDINGS[0], stale_days=1)]
        day9 = [dict(self.FAIL_FINDINGS[0], stale_days=9)]
        self.assertEqual(phs._fingerprint(day1), phs._fingerprint(day9))


class StalenessBudgetTests(unittest.TestCase):
    def test_two_dead_sessions_is_a_fail(self):
        # 26 market-hours (4 sessions) let a dead publisher read as merely slow
        # for most of a week.
        self.assertLessEqual(phs.STALE_FAIL_HOURS, 13.0)
        self.assertGreater(phs.STALE_FAIL_HOURS, phs.STALE_WARN_HOURS)

    def test_fail_finding_reports_wall_clock_days(self):
        now = datetime(2026, 9, 9, 16, 0, tzinfo=UTC)
        payloads = {"data/x.json": {"build_time": "2026-08-17T21:04:00Z"}}
        findings = phs.check_liveness(payloads, now)
        dead = [f for f in findings if f["code"] == "bundle_not_publishing"]
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["stale_days"], 22)
        self.assertIn("22 days ago", dead[0]["detail"])


class LiveBundleTests(unittest.TestCase):
    """The committed bundle must be browser-parseable and completely present."""

    def test_committed_bundle_parses_and_is_complete(self):
        findings, payloads = phs.check_parse_and_presence()
        self.assertEqual(findings, [], f"published bundle has findings: {findings}")
        self.assertGreater(len(payloads), 10)


if __name__ == "__main__":
    unittest.main()
