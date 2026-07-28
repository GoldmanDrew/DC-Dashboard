"""Fail closed when private code, account data, or secrets enter DC-Dashboard."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 40 * 1024 * 1024
ALLOWED_TOP_LEVEL = {
    ".git",
    ".github",
    ".gitignore",
    ".nojekyll",
    "AGENTS.md",
    "README.md",
    "assets",
    "data",
    "index.html",
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
    "data/investors.json",
    "data/dashboard_data.json",
    "data/borrow_history.json",
    "data/etf_metrics_daily.json",
    "data/options_cache.json",
    "data/public_bundle_manifest.json",
}


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


def validate() -> None:
    top_level = {path.name for path in ROOT.iterdir()}
    unexpected = top_level - ALLOWED_TOP_LEVEL
    if unexpected:
        raise AssertionError(f"unexpected top-level paths: {sorted(unexpected)}")

    seen: set[str] = set()
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        seen.add(rel)
        if rel in FORBIDDEN_FILES or FORBIDDEN_PATH_PARTS.intersection(path.parts):
            raise AssertionError(f"private path in public repository: {rel}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise AssertionError(f"oversized public file: {rel}")
        if rel in {"scripts/validate_public_boundary.py", "data/investors.json"}:
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

    investors = json.loads((ROOT / "data/investors.json").read_text(encoding="utf-8"))
    users = investors.get("users")
    allowed_users = {"dgoldman", "dmeis"}
    if not isinstance(users, list) or {
        str(user.get("id", "")).lower() for user in users if isinstance(user, dict)
    } != allowed_users or len(users) != len(allowed_users):
        raise AssertionError("dashboard login must contain exactly dgoldman and dmeis")
    for user in users:
        if set(user) != {"id", "name", "salt_b64", "hash_b64", "iterations"}:
            raise AssertionError("dashboard login contains an unexpected credential field")
        if not user["salt_b64"] or not user["hash_b64"] or int(user["iterations"]) < 250_000:
            raise AssertionError("dashboard login hash is incomplete or too weak")

    manifest = json.loads(
        (ROOT / "data/public_bundle_manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("public_repository") != "GoldmanDrew/DC-Dashboard":
        raise AssertionError("invalid public bundle destination")
    for item in manifest.get("files") or []:
        exported = ROOT / item["path"]
        if not exported.is_file():
            raise AssertionError(f"manifest file missing: {item['path']}")
        digest = hashlib.sha256(exported.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise AssertionError(f"manifest checksum mismatch: {item['path']}")

    print(f"public boundary ok: {len(seen)} files")


if __name__ == "__main__":
    validate()
