"""Fail closed when private code, account data, or secrets enter DC-Dashboard."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 40 * 1024 * 1024
ALLOWED_TOP_LEVEL = {
    ".claude",
    ".git",
    ".github",
    ".gitattributes",
    ".gitignore",
    ".nojekyll",
    ".wrangler",
    "AGENTS.md",
    "README.md",
    "_headers",  # Cloudflare Pages cache policy for the content-hashed bundle
    "assets",
    "build.mjs",  # esbuild: assets/app.jsx -> assets/app.bundle.js
    "data",
    "index.html",
    "node_modules",  # gitignored; esbuild devDependency only
    "package-lock.json",  # gitignored
    "package.json",
    "scripts",
    "tests",
}
FORBIDDEN_PATH_PARTS = {
    "backend",
    "config",
    "dcq",
    "legacy",
    "notebooks",
    "outputs",
    "risk_dashboard",
    "site",
}
FORBIDDEN_FILES = {
    "daily_screener.py",
    "docker-compose.yml",
    "Dockerfile",
    "requirements.txt",
    "run.py",
    "strategy_config.py",
    # This repository is public: everything in it is served by
    # raw.githubusercontent.com regardless of the Cloudflare Access policy on
    # the Worker and Pages hostnames. A PBKDF2 credential file here is a
    # published credential file -- verified 2026-07-30, HTTP 200 anonymous.
    # Access is the real gate; the in-page login was a second, weaker door that
    # leaked its own hashes. Never reintroduce it.
    "data/investors.json",
}
FORBIDDEN_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bCLEARSTREET_(?:CLIENT_SECRET|OLYMPUS_API_KEY|STUDIO_API_TOKEN)\b"),
    re.compile(r"(?i)\b(?:CS_SFTP_PRIVATE_KEY|DC_DASHBOARD_TOKEN)\s*="),
    re.compile(r"(?i)\bstahl\b"),
    re.compile(r"(?i)sftp-static\.clearstreet\.io"),
)
FORBIDDEN_JSON_KEYS = {
    "account_id",
    "clearstreet_account_id",
    "client_id",
    "client_secret",
    "api_key",
    "access_token",
    "refresh_token",
    "password",
    "private_key",
}
REQUIRED_FILES = {
    "index.html",
    "data/dashboard_data.json",
    "data/borrow_history.json",
    "data/etf_metrics_daily.json",
    "data/options_cache.json",
    "data/public_bundle_manifest.json",
}


def _assert_borrow_sign_convention(payload: dict) -> None:
    """Fail closed on HTB-as-rebate / spot-vs-avg polarity clashes.

    Mirrors Diamond-Creek-Execution ``export_public_dashboard._assert_borrow_sign_convention``
    so Pages deploys cannot ship a flipped Olympus publish even if CI bypasses the exporter.
    """
    records = payload.get("records")
    if not isinstance(records, list):
        return
    bad: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or "").strip().upper()
        try:
            spot = float(rec["borrow_current"]) if rec.get("borrow_current") is not None else None
        except (TypeError, ValueError):
            spot = None
        try:
            avg = float(rec["borrow_avg_annual"]) if rec.get("borrow_avg_annual") is not None else None
        except (TypeError, ValueError):
            avg = None
        if spot is None:
            continue
        if spot > 0.50:
            bad.append(f"{sym}:borrow_current={spot:.4f}>0.50")
            continue
        if spot > 0.15 and avg is not None and avg < -0.15:
            bad.append(f"{sym}:spot={spot:.4f}/avg={avg:.4f}")
    if bad:
        raise AssertionError(
            "borrow sign convention check failed (short_favorable_positive): "
            + "; ".join(bad[:12])
            + (f" (+{len(bad) - 12} more)" if len(bad) > 12 else "")
        )


def _walk_json(value: object, rel: str) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            bad = FORBIDDEN_JSON_KEYS.intersection(str(key).lower() for key in current)
            if bad:
                raise AssertionError(f"{rel}: forbidden JSON keys {sorted(bad)}")
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _manifest_file_bytes(path: Path) -> bytes:
    """Hash bytes as committed on Linux CI: normalize text newlines to LF."""
    raw = path.read_bytes()
    if path.suffix.lower() in {".json", ".csv", ".md", ".html", ".js", ".yml", ".yaml", ".txt"}:
        return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return raw


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(_manifest_file_bytes(path)).hexdigest()


def rebuild_public_bundle_manifest() -> dict:
    """Rewrite ``data/public_bundle_manifest.json`` with LF-stable hashes."""
    files: list[dict] = []
    data_root = ROOT / "data"
    for path in sorted(p for p in data_root.rglob("*") if p.is_file()):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "data/public_bundle_manifest.json":
            continue
        payload = _manifest_file_bytes(path)
        files.append(
            {
                "path": rel,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = {
        "schema_version": 1,
        "source": "sanitized-private-export",
        "public_repository": "GoldmanDrew/DC-Dashboard",
        "history_days": 120,
        "max_points_per_symbol": 120,
        "files": files,
    }
    (data_root / "public_bundle_manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def validate() -> None:
    top_level = {path.name for path in ROOT.iterdir()}
    unexpected = top_level - ALLOWED_TOP_LEVEL
    if unexpected:
        raise AssertionError(f"unexpected top-level paths: {sorted(unexpected)}")

    seen: set[str] = set()
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or ".git" in path.parts
            or ".wrangler" in path.parts
            or ".claude" in path.parts
            # gitignored build tooling; never deployed. Scanning third-party
            # sources for "forbidden patterns" only invites false positives, and
            # the esbuild binary would trip the size ceiling.
            or "node_modules" in path.parts
        ):
            continue
        rel = path.relative_to(ROOT).as_posix()
        seen.add(rel)
        if rel in FORBIDDEN_FILES or FORBIDDEN_PATH_PARTS.intersection(path.parts):
            raise AssertionError(f"private path in public repository: {rel}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise AssertionError(f"oversized public file: {rel}")
        if rel == "scripts/validate_public_boundary.py":
            continue
        if path.suffix.lower() in {".html", ".js", ".json", ".csv", ".md", ".yml", ".yaml"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(text):
                    raise AssertionError(f"private value found in {rel}")
            if path.suffix.lower() == ".json":
                payload = json.loads(text)
                _walk_json(payload, rel)

    missing = REQUIRED_FILES - seen
    if missing:
        raise AssertionError(f"missing public dashboard files: {sorted(missing)}")

    dashboard = json.loads((ROOT / "data/dashboard_data.json").read_text(encoding="utf-8"))
    if dashboard.get("source_repo") != "GoldmanDrew/DC-Dashboard":
        raise AssertionError("dashboard_data.json was not sanitized for DC-Dashboard")
    if "last_commit" in dashboard:
        raise AssertionError("private commit metadata leaked into dashboard_data.json")
    _assert_borrow_sign_convention(dashboard)

    # Access control is enforced by Cloudflare Access in front of both the
    # Worker and the Pages hostname, not by anything in this repository. Assert
    # that no credential material has crept back in: a public repo cannot hold
    # a password hash, and a client-side gate over public files never gated
    # anything anyway.
    for candidate in ("data/investors.json", "data/users.json", "data/auth.json"):
        if (ROOT / candidate).exists():
            raise AssertionError(
                f"{candidate} must not exist: this repository is public, so any "
                "credential file in it is world-readable via raw.githubusercontent.com"
            )
    index_text = (ROOT / "index.html").read_text(encoding="utf-8", errors="ignore")
    for token in ("salt_b64", "hash_b64", "PBKDF2", "deriveBits"):
        if token in index_text:
            raise AssertionError(
                f"index.html still references {token}: the in-page credential "
                "gate was removed deliberately; Cloudflare Access is the gate"
            )

    manifest = json.loads(
        (ROOT / "data/public_bundle_manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("public_repository") != "GoldmanDrew/DC-Dashboard":
        raise AssertionError("invalid public bundle destination")
    for item in manifest.get("files") or []:
        exported = ROOT / item["path"]
        if not exported.is_file():
            raise AssertionError(f"manifest file missing: {item['path']}")
        digest = _file_sha256(exported)
        payload = _manifest_file_bytes(exported)
        if len(payload) != int(item.get("bytes") or -1):
            raise AssertionError(
                f"manifest byte-length mismatch: {item['path']} "
                f"(manifest={item.get('bytes')} lf={len(payload)})"
            )
        if digest != item["sha256"]:
            raise AssertionError(f"manifest checksum mismatch: {item['path']}")

    metrics = json.loads((ROOT / "data/etf_metrics_daily.json").read_text(encoding="utf-8"))
    metric_rows = metrics.get("rows") if isinstance(metrics, dict) else None
    if not isinstance(metric_rows, list) or not metric_rows:
        raise AssertionError("etf_metrics_daily.json has no rows")
    metric_dates = [
        str(row.get("date") or "")[:10]
        for row in metric_rows
        if isinstance(row, dict) and str(row.get("date") or "")[:10]
    ]
    if not metric_dates:
        raise AssertionError("etf_metrics_daily.json has no dated rows")
    metrics_latest = max(metric_dates)
    adj_days = sum(
        1
        for row in metric_rows
        if isinstance(row, dict)
        and isinstance(row.get("etf_adj_close"), (int, float))
        and float(row["etf_adj_close"]) > 0
    )
    if adj_days < max(1, int(len(metric_rows) * 0.5)):
        raise AssertionError(
            f"etf_metrics_daily.json missing etf_adj_close "
            f"({adj_days}/{len(metric_rows)} positive)"
        )

    freshness = json.loads((ROOT / "data/freshness_summary.json").read_text(encoding="utf-8"))
    claimed = str((freshness.get("metrics") or {}).get("latest_date") or "")[:10]
    if claimed and claimed != metrics_latest:
        raise AssertionError(
            f"freshness_summary metrics.latest_date={claimed} "
            f"does not match etf_metrics_daily max date={metrics_latest}"
        )

    print(f"public boundary ok: {len(seen)} files")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild-manifest",
        action="store_true",
        help="Rewrite public_bundle_manifest.json with LF-normalized hashes, then validate",
    )
    args = parser.parse_args()
    if args.rebuild_manifest:
        rebuild_public_bundle_manifest()
        print("rewrote data/public_bundle_manifest.json")
    validate()
