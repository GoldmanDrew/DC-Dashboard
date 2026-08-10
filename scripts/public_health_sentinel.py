#!/usr/bin/env python3
"""Health sentinel for the published public dashboard bundle.

`validate_public_boundary.py` answers "is this bundle *allowed* to be public and
internally consistent?" — forbidden paths, credential material, sanitization,
and manifest sha256/byte agreement. It runs on push, as a pre-commit gate.

This script answers a different question: **"is the published bundle still alive
and still sane?"** Those are the failure modes the boundary gate cannot see:

  * the bundle stops arriving at all. Publishing runs locally
    (`chore: market tick (local)` in the private repo), so a laptop that stops
    publishing leaves the public site serving stale data forever. Nothing in
    this repo is scheduled, so nothing notices.
  * a payload carries a bare `NaN`/`Infinity` token. Python's `json.loads`
    accepts those; the browser's `JSON.parse` does not. That combination once
    blanked an entire dashboard: producer-side "valid" is not consumer-side
    parseable.
  * a bundle is internally consistent but wrong — the manifest is regenerated
    from disk at publish time, so it proves "what shipped is what we meant to
    ship", never "what shipped is correct". A bundle with 50 records instead of
    545 passes every hash check. Only a comparison against the *previous*
    published bundle catches that.

Design constraints for this repository:
  * stdlib only — `requirements.txt` is in the boundary validator's
    FORBIDDEN_FILES, so there is nowhere to declare a dependency;
  * **never writes into the repository.** A commit here triggers a Cloudflare
    Pages deploy, and any new file under `data/` would be swept into the bundle
    manifest. Findings go to stdout, the job summary, and a GitHub issue. The
    report is written only to a caller-supplied path outside the repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
MANIFEST = DATA / "public_bundle_manifest.json"
ISSUE_LABEL = "ops/public-health"

FAIL = "fail"
WARN = "warn"

# Bundle-wide liveness budgets, in market hours (weekends and NYSE holidays are
# not counted — publishing legitimately stops then).
STALE_WARN_HOURS = 6.0
STALE_FAIL_HOURS = 26.0
# Spread between the newest and oldest build_time inside one bundle, in market
# hours. Deliberately loose: the newest artifact refreshes every tick while
# several others are on a daily cadence, so any daily artifact looks ~24h
# "behind" by the end of a session. This must catch *frozen*, not *slower* —
# two full trading days without a refresh.
SKEW_WARN_HOURS = 48.0
# A published artifact losing more than this share of its records against the
# previous published bundle is a regression the manifest cannot detect.
RECORD_DROP_FRAC = 0.50
BYTE_DROP_FRAC = 0.60

# Collection locator per artifact, for record-count regression. Mirrors the
# shapes the browser actually consumes; anything absent here is still size- and
# parse-checked.
RECORD_LOCATORS: dict[str, str] = {
    "data/dashboard_data.json": "records",
    "data/underlying_intraday_spot.json": "by_symbol",
    "data/vrp_live.json": "rows",
    "data/nav_forecasts/_latest.json": "by_symbol",
    "data/borrow_history.json": "symbols",
    "data/options_cache.json": "symbols",
    "data/letf_rebalance_flows_intraday_latest.json": "by_fund",
    "data/letf_rebalance_flows_latest.json": "by_ticker",
    "data/corporate_actions.json": "events",
    "data/etf_metrics_daily.json": "rows",
    "data/yieldboost_put_spreads_latest.json": "spreads",
}

# Files index.html/app.bundle.js fetch at runtime. Losing one degrades a panel
# with no server-side signal, so absence is a finding even though the boundary
# validator's REQUIRED_FILES set is narrower.
RUNTIME_FETCHED = (
    "data/dashboard_data.json",
    "data/borrow_history.json",
    "data/options_cache.json",
    "data/underlying_intraday_spot.json",
    "data/vrp_live.json",
    "data/vrp_health.json",
    "data/freshness_summary.json",
    "data/etf_metrics_daily.json",
    "data/nav_forecasts/_latest.json",
    "data/letf_rebalance_flows_latest.json",
    "data/letf_rebalance_flows_intraday_latest.json",
    "data/corporate_actions.json",
    "data/product_taxonomy.json",
)


# ---------------------------------------------------------------------------
# Minimal NYSE session calendar (stdlib only).
# Full-day closures only; early closes are irrelevant at these hour budgets.

def _easter(year: int) -> date:
    """Meeus/Jones/Butcher — Good Friday is Easter minus two days."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, 0
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    if d.weekday() == 5:      # Saturday -> observed Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:      # Sunday -> observed Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, 31) if month == 5 else date(year, month, 30)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set[date]:
    days = {
        _observed(date(year, 1, 1)),                    # New Year's Day
        _nth_weekday(year, 1, 0, 3),                    # MLK
        _nth_weekday(year, 2, 0, 3),                    # Washington's Birthday
        _easter(year) - timedelta(days=2),              # Good Friday
        _last_weekday(year, 5, 0),                      # Memorial Day
        _observed(date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(date(year, 12, 25)),                  # Christmas
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))          # Juneteenth
    return days


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def market_age_hours(ts: datetime, now: datetime) -> float:
    """Elapsed hours excluding whole non-session days."""
    if ts >= now:
        return 0.0
    hours = (now - ts).total_seconds() / 3600.0
    d = ts.date() + timedelta(days=1)
    skipped = 0
    while d < now.date():
        if not is_session(d):
            skipped += 1
        d += timedelta(days=1)
    return max(0.0, hours - 24.0 * skipped)


# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def finding(severity: str, code: str, artifact: str, detail: str,
            observed=None, threshold=None) -> dict:
    out = {"severity": severity, "code": code, "artifact": artifact, "detail": detail}
    if observed is not None:
        out["observed"] = observed
    if threshold is not None:
        out["threshold"] = threshold
    return out


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON token {name!r}")


def load_browser_strict(path: Path):
    """Parse with browser semantics: bare NaN/Infinity is an error, as in JSON.parse."""
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def git_show(ref: str, rel: str) -> bytes | None:
    try:
        proc = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=ROOT,
                              capture_output=True, timeout=120)
    except Exception:
        return None
    return proc.stdout if proc.returncode == 0 else None


def data_tree_dirty() -> bool:
    try:
        proc = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "data"],
                              cwd=ROOT, capture_output=True, timeout=60)
    except Exception:
        return False
    return proc.returncode != 0


def count_records(payload, locator: str | None) -> int | None:
    if not locator or not isinstance(payload, dict):
        return None
    node = payload
    for part in locator.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return len(node) if isinstance(node, (list, dict)) else None


# ---------------------------------------------------------------------------
# Checks

def check_parse_and_presence() -> tuple[list[dict], dict[str, object]]:
    """Browser-strict parse of every published JSON, plus runtime-file presence."""
    findings: list[dict] = []
    payloads: dict[str, object] = {}
    for path in sorted(DATA.rglob("*.json")):
        rel = path.relative_to(ROOT).as_posix()
        try:
            payloads[rel] = load_browser_strict(path)
        except Exception as exc:
            findings.append(finding(
                FAIL, "browser_unparseable", rel,
                f"not parseable by the browser: {exc}. Python's json.loads accepts bare "
                "NaN/Infinity but JSON.parse rejects them — this blanks the panel that "
                "fetches it (and the whole app for dashboard_data.json)."))
    for rel in RUNTIME_FETCHED:
        p = ROOT / rel
        if not p.is_file():
            findings.append(finding(
                FAIL, "runtime_file_missing", rel,
                "fetched by the dashboard at runtime but absent from the bundle"))
        elif p.stat().st_size == 0:
            findings.append(finding(FAIL, "runtime_file_empty", rel, "published as zero bytes"))
    return findings, payloads


def check_manifest_coverage(payloads: dict[str, object]) -> list[dict]:
    """Every published file listed, and nothing published that is not listed.

    The boundary validator verifies listed->present with hashes; the reverse
    direction is the leak-adjacent one: a file that appears under data/ without
    entering the manifest still deploys to the public site.
    """
    findings: list[dict] = []
    manifest = payloads.get("data/public_bundle_manifest.json")
    if not isinstance(manifest, dict):
        return [finding(FAIL, "manifest_unreadable", "data/public_bundle_manifest.json",
                        "bundle manifest missing or unparseable")]
    listed = {str(item.get("path")) for item in manifest.get("files") or []}
    on_disk = {p.relative_to(ROOT).as_posix() for p in DATA.rglob("*") if p.is_file()}
    on_disk.discard("data/public_bundle_manifest.json")
    unlisted = sorted(on_disk - listed)
    if unlisted:
        findings.append(finding(
            FAIL, "unlisted_published_file", "data/public_bundle_manifest.json",
            f"{len(unlisted)} file(s) under data/ are absent from the bundle manifest yet "
            f"deploy publicly: {', '.join(unlisted[:6])}", observed=unlisted[:20]))
    missing = sorted(listed - on_disk)
    if missing:
        findings.append(finding(
            FAIL, "manifest_file_missing", "data/public_bundle_manifest.json",
            f"manifest lists {len(missing)} file(s) not present: {', '.join(missing[:6])}"))
    return findings


def check_liveness(payloads: dict[str, object], now: datetime) -> list[dict]:
    """Has the bundle stopped arriving, and is any artifact frozen behind the rest?"""
    findings: list[dict] = []
    stamps: dict[str, datetime] = {}
    for rel, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        ts = parse_ts(payload.get("build_time"))
        if ts is None:
            meta = payload.get("meta")
            if isinstance(meta, dict):
                ts = parse_ts(meta.get("build_time"))
        if ts is not None:
            stamps[rel] = ts
    if not stamps:
        return [finding(FAIL, "no_build_times", "data/",
                        "no artifact carries a build_time — cannot establish liveness")]

    newest_rel, newest = max(stamps.items(), key=lambda kv: kv[1])
    age = market_age_hours(newest, now)
    if age > STALE_FAIL_HOURS:
        findings.append(finding(
            FAIL, "bundle_not_publishing", "data/",
            f"newest artifact in the bundle ({newest_rel}) is {age:.1f} market-hours old "
            f"({iso_z(newest)}). Publishing runs locally, so this usually means the "
            "publisher stopped — the public site is serving stale data.",
            observed=round(age, 1), threshold=STALE_FAIL_HOURS))
    elif age > STALE_WARN_HOURS:
        findings.append(finding(
            WARN, "bundle_stale", "data/",
            f"no artifact newer than {age:.1f} market-hours ({newest_rel} at {iso_z(newest)})",
            observed=round(age, 1), threshold=STALE_WARN_HOURS))

    oldest_rel, oldest = min(stamps.items(), key=lambda kv: kv[1])
    # Market hours, not wall clock: an artifact last built on Friday is ~5h behind
    # a Monday bundle, not 53h. Wall-clock skew fires every Monday for nothing.
    skew = market_age_hours(oldest, newest)
    if skew > SKEW_WARN_HOURS:
        findings.append(finding(
            WARN, "bundle_skew", oldest_rel,
            f"{oldest_rel} is {skew:.1f}h behind the newest artifact in the same bundle "
            f"({iso_z(oldest)} vs {iso_z(newest)}) — it may have stopped refreshing while "
            "the rest of the bundle keeps moving",
            observed=round(skew, 1), threshold=SKEW_WARN_HOURS))
    return findings


def check_regression(payloads: dict[str, object], baseline: str) -> list[dict]:
    """Compare this bundle against the previously published one.

    The manifest is rebuilt from disk at publish time, so it can never reveal a
    content regression — only this comparison can.
    """
    findings: list[dict] = []
    for rel, payload in sorted(payloads.items()):
        if rel == "data/public_bundle_manifest.json":
            continue
        raw = git_show(baseline, rel)
        if not raw:
            continue
        path = ROOT / rel
        new_bytes = path.stat().st_size
        old_bytes = len(raw)
        if old_bytes > 4096 and new_bytes < old_bytes * (1.0 - BYTE_DROP_FRAC):
            findings.append(finding(
                FAIL, "byte_size_regression", rel,
                f"shrank {old_bytes:,}B -> {new_bytes:,}B versus the previous published "
                f"bundle (>{BYTE_DROP_FRAC:.0%} drop)",
                observed=new_bytes, threshold=int(old_bytes * (1.0 - BYTE_DROP_FRAC))))
        locator = RECORD_LOCATORS.get(rel)
        if not locator:
            continue
        try:
            old_payload = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        old_n = count_records(old_payload, locator)
        new_n = count_records(payload, locator)
        if old_n and new_n is not None and old_n >= 10 and new_n < old_n * (1.0 - RECORD_DROP_FRAC):
            findings.append(finding(
                FAIL, "record_count_regression", rel,
                f"{locator} fell {old_n} -> {new_n} versus the previous published bundle "
                f"(>{RECORD_DROP_FRAC:.0%} drop) — hashes still match, so only this "
                "comparison can catch it",
                observed=new_n, threshold=int(old_n * (1.0 - RECORD_DROP_FRAC))))
    return findings


# ---------------------------------------------------------------------------

def verdict_of(findings: list[dict]) -> str:
    if any(f["severity"] == FAIL for f in findings):
        return FAIL
    if findings:
        return WARN
    return "pass"


def run_check(baseline: str | None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    if baseline is None:
        # A dirty data/ tree means we are validating an unpublished bundle, so
        # HEAD is the previous one. A clean tree means HEAD *is* the bundle under
        # test, so the comparison point is the publish before it.
        baseline = "HEAD" if data_tree_dirty() else "HEAD~1"
    findings, payloads = check_parse_and_presence()
    findings += check_manifest_coverage(payloads)
    findings += check_liveness(payloads, now)
    findings += check_regression(payloads, baseline)
    return {
        "schema_v": 1,
        "checked_at": iso_z(now),
        "baseline": baseline,
        "verdict": verdict_of(findings),
        "stats": {
            "files_checked": len(payloads),
            "fail": sum(1 for f in findings if f["severity"] == FAIL),
            "warn": sum(1 for f in findings if f["severity"] == WARN),
        },
        "findings": findings,
    }


def _fingerprint(findings: list[dict]) -> str:
    key = json.dumps(sorted((f["code"], f["artifact"], f["severity"]) for f in findings))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


_MARKER_RE = re.compile(r"<!-- public-health fp=(?P<fp>\w+) verdict=(?P<verdict>\w+) -->")


def cmd_check(args) -> int:
    report = run_check(args.baseline)
    for f in report["findings"]:
        tag = "::error" if f["severity"] == FAIL else "::warning"
        print(f"{tag} title=public-health {f['code']}::{f['artifact']}: {f['detail']}")
    print(f"[public-health] verdict={report['verdict']} "
          f"files={report['stats']['files_checked']} "
          f"fail={report['stats']['fail']} warn={report['stats']['warn']} "
          f"baseline={report['baseline']}")

    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [f"### Public health: `{report['verdict']}`", "",
                 f"{report['stats']['files_checked']} files checked against `{report['baseline']}`.", ""]
        for f in report["findings"]:
            lines.append(f"- **{f['severity']}** `{f['code']}` {f['artifact']} — {f['detail']}")
        if not report["findings"]:
            lines.append("No findings.")
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    return 1 if (args.strict and report["verdict"] == FAIL) else 0


def cmd_alert(args) -> int:
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"no report to alert on ({exc})", file=sys.stderr)
        return 0

    def gh(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)

    def open_issue() -> int | None:
        if os.environ.get("GITHUB_ACTIONS") != "true" and not os.environ.get("GH_TOKEN"):
            return None
        proc = gh(["gh", "issue", "list", "--label", ISSUE_LABEL, "--state", "open",
                   "--json", "number", "--limit", "5"])
        if proc.returncode != 0:
            print(proc.stderr or proc.stdout, file=sys.stderr)
            return None
        try:
            items = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return None
        return next((i["number"] for i in items if isinstance(i.get("number"), int)), None)

    findings = report.get("findings", [])
    verdict = report.get("verdict", "pass")
    fp = _fingerprint(findings)
    marker = f"<!-- public-health fp={fp} verdict={verdict} -->"

    if verdict == "pass":
        num = None if args.dry_run else open_issue()
        if num is None:
            print("public health ok — no alert")
            return 0
        gh(["gh", "issue", "comment", str(num), "--body",
            f"Public bundle healthy again as of `{report.get('checked_at')}`. Auto-closing.\n\n{marker}"])
        proc = gh(["gh", "issue", "close", str(num)])
        print(f"closed issue #{num}" if proc.returncode == 0 else proc.stderr)
        return 0

    title = f"Public dashboard health: {verdict} ({len(findings)} finding(s))"
    body = "\n".join(
        [f"## Public bundle health — `{verdict}`", "",
         f"Checked `{report.get('checked_at')}` against baseline `{report.get('baseline')}`.", ""]
        + [f"- **{f['severity']}** `{f['code']}` {f['artifact']} — {f['detail']}" for f in findings]
        + ["", "---",
           "Filed by `scripts/public_health_sentinel.py`. Auto-closes when a later run passes.",
           "", marker])

    if args.dry_run:
        print(f"[dry-run] would alert: {title}")
        for f in findings:
            print(f"  - {f['severity']} {f['code']} {f['artifact']}")
        return 0

    num = open_issue()
    if num is not None:
        view = gh(["gh", "issue", "view", str(num), "--json", "comments,body"])
        if view.returncode == 0:
            try:
                payload = json.loads(view.stdout or "{}")
                texts = [payload.get("body") or ""] + [
                    c.get("body") or "" for c in payload.get("comments") or []]
                marks = [m.groupdict() for t in texts for m in _MARKER_RE.finditer(t)]
                if marks and marks[-1].get("fp") == fp:
                    print(f"issue #{num}: unchanged findings (fp={fp}) — no duplicate comment")
                    return 0
            except json.JSONDecodeError:
                pass
        proc = gh(["gh", "issue", "comment", str(num), "--body",
                   f"**Update {report.get('checked_at')}** — `{verdict}`\n\n"
                   + "\n".join(f"- `{f['code']}` {f['artifact']} — {f['detail']}"
                               for f in findings[:30]) + f"\n\n{marker}"])
        print(f"commented on issue #{num}" if proc.returncode == 0 else proc.stderr)
        return 0
    # gh refuses to create an issue with a label that does not exist yet.
    gh(["gh", "label", "create", ISSUE_LABEL, "--color", "D93F0B",
        "--description", "Public dashboard bundle health", "--force"])
    proc = gh(["gh", "issue", "create", "--title", title, "--body", body, "--label", ISSUE_LABEL])
    print(proc.stdout.strip() if proc.returncode == 0 else (proc.stderr or proc.stdout))
    return 0 if proc.returncode == 0 else proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    chk = sub.add_parser("check", help="validate the published bundle")
    chk.add_argument("--baseline", default=None,
                     help="git ref for regression comparison (default: auto — HEAD when "
                          "data/ is dirty, else HEAD~1)")
    chk.add_argument("--report-out", type=Path, default=None,
                     help="write the JSON report here. Never point this inside the "
                          "repository: a commit triggers a deploy and data/ files are "
                          "swept into the bundle manifest.")
    chk.add_argument("--strict", action="store_true",
                     help="exit 1 when the verdict is fail")
    chk.set_defaults(func=cmd_check)

    alert = sub.add_parser("alert", help="file/update/close the ops/public-health issue")
    alert.add_argument("--report", type=Path, required=True)
    alert.add_argument("--dry-run", action="store_true")
    alert.set_defaults(func=cmd_alert)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
