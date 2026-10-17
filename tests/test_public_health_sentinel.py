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

    def test_market_age_counts_the_open_window_only(self):
        # Friday 17:00 EDT (after the close) to Monday 10:00 EDT. The old
        # formula billed raw wall-clock minus 48h = 17h, which is Friday
        # evening plus Monday pre-open. Only 09:30–10:00 Monday counts.
        friday = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)
        monday = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
        self.assertAlmostEqual(phs.market_age_hours(friday, monday), 0.5, places=6)

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


class SessionClockTests(unittest.TestCase):
    """Age is NYSE regular-session time, and the 6h/13h budgets stay put.

    Dropping 24h only for non-session days strictly between the artifact and
    today billed the in-progress Saturday in full. That failed every weekend
    (issue #14, and the same alarm on Sep 26, Oct 3, and Oct 9) and the same
    way on Monday before the open and the morning after a holiday.
    """

    # The bundle that opened issue #14: Friday 16:24 EDT, after the close.
    FRIDAY_CLOSE = "2026-10-16T20:24:58Z"

    def _findings(self, build: str, now: datetime) -> list[dict]:
        return phs.check_liveness({"data/a.json": {"build_time": build}}, now)

    def _codes(self, build: str, now: datetime) -> list[str]:
        return [f["code"] for f in self._findings(build, now)]

    def test_saturday_after_the_close_is_not_stale(self):
        # Public-health run that opened #14, 2026-10-17 17:56Z. Wall clock was
        # 21.5h and Saturday was not subtracted, so the verdict was fail.
        now = datetime(2026, 10, 17, 17, 56, 14, tzinfo=UTC)
        self.assertAlmostEqual(
            phs.market_age_hours(phs.parse_ts(self.FRIDAY_CLOSE), now), 0.0, places=6)
        self.assertEqual(self._codes(self.FRIDAY_CLOSE, now), [])

    def test_sunday_after_the_close_is_not_stale(self):
        now = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)
        self.assertEqual(self._codes(self.FRIDAY_CLOSE, now), [])

    def test_monday_pre_open_is_not_stale(self):
        now = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)  # 09:00 EDT
        self.assertAlmostEqual(
            phs.market_age_hours(phs.parse_ts(self.FRIDAY_CLOSE), now), 0.0, places=6)
        self.assertEqual(self._codes(self.FRIDAY_CLOSE, now), [])

    def test_monday_after_open_with_stale_data_fails(self):
        # Silent since the Wednesday close. Thursday and Friday are two full
        # sessions (13.0h). Weekend hours must not add to that, so Monday
        # before the open is a warn; Monday 10:00 EDT is 13.5h and fails.
        # The fail threshold is still 13.
        build = "2026-10-14T20:00:00Z"  # Wed 16:00 EDT
        pre = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)   # Mon 09:00 EDT
        after = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # Mon 10:00 EDT
        self.assertAlmostEqual(phs.market_age_hours(phs.parse_ts(build), pre), 13.0, places=6)
        self.assertEqual(self._codes(build, pre), ["bundle_stale"])
        out = self._findings(build, after)
        self.assertEqual([f["code"] for f in out], ["bundle_not_publishing"])
        self.assertEqual(out[0]["severity"], phs.FAIL)
        self.assertEqual(out[0]["observed"], 13.5)
        self.assertEqual(out[0]["threshold"], 13.0)

    def test_holiday_and_the_next_morning_are_not_stale(self):
        # Labor Day 2026-09-07. A Friday close must not warn or fail on the
        # holiday itself, or on Tuesday before the open.
        build = "2026-09-04T20:24:00Z"  # Fri 16:24 EDT
        holiday = datetime(2026, 9, 7, 17, 56, tzinfo=UTC)  # Mon 13:56 EDT
        morning = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)   # Tue 09:00 EDT
        opened = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)    # Tue 10:00 EDT
        self.assertFalse(phs.is_session(holiday.astimezone(phs.NY_TZ).date()))
        self.assertEqual(self._codes(build, holiday), [])
        self.assertEqual(self._codes(build, morning), [])
        # Half an hour into the next session. The holiday contributes nothing.
        self.assertAlmostEqual(
            phs.market_age_hours(phs.parse_ts(build), opened), 0.5, places=6)
        self.assertEqual(self._codes(build, opened), [])

    def test_fail_budget_is_still_thirteen_session_hours(self):
        self.assertEqual(phs.STALE_FAIL_HOURS, 13.0)
        self.assertEqual(phs.STALE_WARN_HOURS, 6.0)
        # Died at the Wednesday open. Exactly two sessions later is not yet a
        # fail; six minutes into the next session (13.1h) is.
        build = "2026-10-14T13:30:00Z"  # Wed 09:30 EDT
        at_two = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)   # Thu 16:00 EDT
        over = datetime(2026, 10, 16, 13, 36, tzinfo=UTC)    # Fri 09:36 EDT
        self.assertAlmostEqual(phs.market_age_hours(phs.parse_ts(build), at_two), 13.0, places=6)
        self.assertEqual(self._codes(build, at_two), ["bundle_stale"])
        out = self._findings(build, over)
        self.assertEqual(out[0]["code"], "bundle_not_publishing")
        self.assertEqual(out[0]["severity"], phs.FAIL)
        self.assertEqual(out[0]["observed"], 13.1)
        self.assertEqual(out[0]["threshold"], phs.STALE_FAIL_HOURS)

    def test_winter_monday_pre_open_uses_eastern_standard_time(self):
        # 2026-11-09 14:00 UTC is 09:00 EST, before the open. A fixed EDT
        # offset would treat it as 10:00 and count half an hour.
        build = "2026-11-06T21:30:00Z"  # Fri 16:30 EST, after the close
        now = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)
        self.assertAlmostEqual(phs.market_age_hours(phs.parse_ts(build), now), 0.0, places=6)
        self.assertEqual(self._codes(build, now), [])

    def test_hours_after_the_close_do_not_accumulate(self):
        start = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)  # Fri 14:00 EDT
        close = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)  # Fri 16:00 EDT
        saturday = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
        self.assertAlmostEqual(phs.market_age_hours(start, close), 2.0, places=6)
        self.assertAlmostEqual(phs.market_age_hours(start, saturday), 2.0, places=6)


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
        # Friday 11:30 EDT through Monday 13:00 EDT is 8.0 session hours:
        # past one session, short of two.
        out = phs.check_liveness(
            self._payloads({"data/a.json": "2026-08-07T15:30:00Z"}), self.NOW)
        self.assertEqual([f["code"] for f in out], ["bundle_stale"])
        self.assertEqual(out[0]["severity"], phs.WARN)
        self.assertEqual(out[0]["observed"], 8.0)

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
        self.assertEqual(phs.count_records({"columns": {"date": ["a", "b"]}}, "rows"), 2)
        self.assertEqual(
            phs.count_records({"rows": [1], "columns": {"date": [1, 2, 3]}}, "rows"), 1)

    def test_columnar_compaction_is_not_a_byte_regression(self):
        old = {"rows": list(range(20))}
        new = {"columns": {"date": ["2026-01-01"] * 20}}
        out = phs.compare_artifact(
            "data/etf_metrics_daily.json", "rows",
            new_bytes=1_000, old_bytes=100_000, new_payload=new, old_payload=old)
        self.assertEqual(out, [])

    def test_byte_drop_without_a_held_record_count_still_fails(self):
        out = phs.compare_artifact(
            "data/x.json", None,
            new_bytes=1_000, old_bytes=100_000, new_payload={}, old_payload={})
        self.assertEqual([f["code"] for f in out], ["byte_size_regression"])

    def test_record_drop_still_fails(self):
        old = {"rows": list(range(20))}
        new = {"rows": [1, 2]}
        out = phs.compare_artifact(
            "data/etf_metrics_daily.json", "rows",
            new_bytes=80_000, old_bytes=100_000, new_payload=new, old_payload=old)
        self.assertEqual([f["code"] for f in out], ["record_count_regression"])


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
