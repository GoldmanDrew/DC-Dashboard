"""Columnar public encoding for ``data/etf_metrics_daily.json``.

The file is produced by the private repository's local publisher
(``export_public_dashboard`` in Diamond-Creek-Execution, commit subject
``publish sanitized dashboard bundle (local runner)``). That exporter is not
in this repository. It must write the shape below; this module is the
contract and the converter for a row-oriented file that is already here.

Cloudflare Pages rejects any one file over 25 MiB (``Pages only supports
files up to 25 MiB``). A row array repeats ~20 field names on every
observation and embeds pipeline provenance URLs, which pushed the public
file past that cap. ``columnar_v1`` stores each field once as a parallel
array. The dashboard expands those arrays back into the row objects the
rest of the UI already reads (``assets/etf_metrics_format.js``).

Publisher rules
---------------

* Top-level object, compact JSON (no extra whitespace), no bare
  ``NaN`` / ``Infinity`` (the browser's ``JSON.parse`` rejects them)::

      {
        "format": "columnar_v1",
        "build_time": "<iso8601>",
        "public_history_days": 120,
        "tickers": ["AAA", "BBB"],
        "columns": { "<field>": [ ... one entry per observation ... ] }
      }

* ``tickers`` is the sorted unique ticker list. ``columns.ticker_ix`` is an
  integer index into that list. Every column has the same length. Observation
  order is the publisher's order; the dashboard sorts each symbol by date.

* Do **not** emit ``source_url``, ``ingested_at_utc``, or ``source_provider``.
  Those are pipeline provenance. Carry-forward detection in the dashboard
  already keys off ``stale_kind == "carry_forward"`` (and ``stale``). In the
  2026-10-16 bundle every carry-forward row has that ``stale_kind``, and no
  symbol has a calendar gap above the 45-day source-change cut, so dropping
  the three fields does not move contiguous-window metrics.

* Round before writing, half away from zero (``ROUND_HALF_UP``):

  - prices, 4 decimal places: ``close_price``, ``nav``, ``etf_adj_close``,
    ``underlying_adj_close``
  - integers: ``aum``, ``shares_outstanding``, ``shares_traded``
  - ``stale_age_bdays``: integer, or null

* ``etf_adj_close`` is the Yahoo total-return series. The dashboard needs it
  when it differs from ``close_price`` (splits and distributions). When the
  rounded values are equal, store JSON ``null`` and the loader copies
  ``close_price`` back. When ``close_price`` is present and the adjusted
  close is genuinely missing, do **not** use null for that — null would be
  read as "same as close". List those row indexes in
  ``etf_adj_close_missing`` (omit the key when the list is empty).

* Keep the other public fields: ``date``, ``nav``, ``aum``,
  ``shares_outstanding``, ``shares_traded``, ``status``, ``stale``,
  ``stale_kind``, ``stale_age_bdays``, ``underlying_adj_close``,
  ``issuer_asof_date``, ``market_asof_date``, ``premium_discount_eligible``,
  ``premium_discount_status``.

* Stay under 20 MiB. ``scripts/validate_public_boundary.py`` fails the deploy
  at that size, short of the Pages 25 MiB cap.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

FORMAT = "columnar_v1"

# Pipeline bookkeeping. Never written to the public file.
PROVENANCE_KEYS = ("source_url", "ingested_at_utc", "source_provider")

PRICE_KEYS = ("close_price", "nav", "etf_adj_close", "underlying_adj_close")
INTEGER_KEYS = ("aum", "shares_outstanding", "shares_traded")

COLUMN_KEYS = (
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
)

_PRICE_Q = Decimal("0.0001")
_INT_Q = Decimal("1")


class MetricsFormatError(ValueError):
    """The public metrics file is not readable columnar_v1."""


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetricsFormatError(f"expected a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise MetricsFormatError(f"non-finite number {value!r}")
    return Decimal(str(value))


def _json_number(quantized: Decimal) -> int | float:
    """A JSON number whose default encoding is the quantized decimal."""
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return json.loads(text)


def round_price(value: object) -> int | float | None:
    if value is None:
        return None
    quantized = _decimal(value).quantize(_PRICE_Q, rounding=ROUND_HALF_UP)
    return _json_number(quantized)


def round_int(value: object) -> int | None:
    if value is None:
        return None
    return int(_decimal(value).quantize(_INT_Q, rounding=ROUND_HALF_UP))


def _as_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def expand(payload: object) -> list[dict]:
    """Row objects the dashboard already consumes.

    A legacy ``rows`` array is returned unchanged so a local file from before
    the columnar cut still loads. The published file must be ``columnar_v1``;
    ``validate_public_boundary.py`` enforces that.
    """
    if not isinstance(payload, dict):
        raise MetricsFormatError("etf metrics payload must be a JSON object")
    legacy = payload.get("rows")
    if payload.get("format") != FORMAT and isinstance(legacy, list):
        return [row for row in legacy if isinstance(row, dict)]
    if payload.get("format") != FORMAT:
        raise MetricsFormatError(
            "etf metrics must be columnar_v1 or a legacy rows array"
        )
    return expand_columnar(payload)


def expand_columnar(payload: dict) -> list[dict]:
    columns = payload.get("columns")
    if not isinstance(columns, dict):
        raise MetricsFormatError("columnar_v1 metrics missing columns")
    tickers = payload.get("tickers")
    if not isinstance(tickers, list) or not all(isinstance(t, str) and t for t in tickers):
        raise MetricsFormatError("columnar_v1 tickers must be a list of strings")

    arrays: dict[str, list] = {}
    n: int | None = None
    for key in COLUMN_KEYS:
        values = columns.get(key)
        if not isinstance(values, list):
            raise MetricsFormatError(f"columnar_v1 column {key} must be an array")
        if n is None:
            n = len(values)
        elif len(values) != n:
            raise MetricsFormatError(
                f"columnar_v1 column {key} has length {len(values)}, expected {n}"
            )
        arrays[key] = values
    assert n is not None

    missing_raw = payload.get("etf_adj_close_missing") or []
    if not isinstance(missing_raw, list):
        raise MetricsFormatError("etf_adj_close_missing must be an array of indexes")
    missing: set[int] = set()
    for index in missing_raw:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < n:
            raise MetricsFormatError(f"etf_adj_close_missing index out of range: {index!r}")
        missing.add(index)

    rows: list[dict] = []
    for i in range(n):
        ticker_ix = arrays["ticker_ix"][i]
        if isinstance(ticker_ix, bool) or not isinstance(ticker_ix, int) or not 0 <= ticker_ix < len(tickers):
            raise MetricsFormatError(f"ticker_ix out of range at row {i}: {ticker_ix!r}")
        close = arrays["close_price"][i]
        adj = arrays["etf_adj_close"][i]
        # null adjusted close means "same as close" unless this row is listed
        # as genuinely missing. A stored number always wins.
        if adj is None and close is not None and i not in missing:
            adj = close
        rows.append(
            {
                "date": arrays["date"][i],
                "ticker": tickers[ticker_ix],
                "nav": arrays["nav"][i],
                "aum": arrays["aum"][i],
                "shares_outstanding": arrays["shares_outstanding"][i],
                "shares_traded": arrays["shares_traded"][i],
                "status": arrays["status"][i],
                "stale": arrays["stale"][i],
                "stale_age_bdays": arrays["stale_age_bdays"][i],
                "stale_kind": arrays["stale_kind"][i],
                "close_price": close,
                "etf_adj_close": adj,
                "underlying_adj_close": arrays["underlying_adj_close"][i],
                "issuer_asof_date": arrays["issuer_asof_date"][i],
                "market_asof_date": arrays["market_asof_date"][i],
                "premium_discount_eligible": arrays["premium_discount_eligible"][i],
                "premium_discount_status": arrays["premium_discount_status"][i],
            }
        )
    return rows


def _rounded_row(row: dict) -> dict:
    out = {
        "date": row.get("date"),
        "ticker": str(row.get("ticker") or "").upper(),
        "status": row.get("status"),
        "stale": _as_bool(row.get("stale")),
        "stale_kind": row.get("stale_kind"),
        "issuer_asof_date": row.get("issuer_asof_date"),
        "market_asof_date": row.get("market_asof_date"),
        "premium_discount_eligible": _as_bool(row.get("premium_discount_eligible")),
        "premium_discount_status": row.get("premium_discount_status"),
    }
    for key in PRICE_KEYS:
        out[key] = round_price(row.get(key))
    for key in INTEGER_KEYS:
        out[key] = round_int(row.get(key))
    age = row.get("stale_age_bdays")
    out["stale_age_bdays"] = None if age is None else round_int(age)
    return out


def compact(payload: dict) -> dict:
    """Build a ``columnar_v1`` object from row-oriented or columnar input."""
    if not isinstance(payload, dict):
        raise MetricsFormatError("etf metrics payload must be a JSON object")
    if payload.get("format") == FORMAT and not isinstance(payload.get("rows"), list):
        source_rows = expand_columnar(payload)
    else:
        legacy = payload.get("rows")
        if not isinstance(legacy, list):
            raise MetricsFormatError("etf metrics has neither rows nor columnar_v1 columns")
        source_rows = [row for row in legacy if isinstance(row, dict)]

    rounded = [_rounded_row(row) for row in source_rows]
    tickers = sorted({row["ticker"] for row in rounded if row["ticker"]})
    index_of = {ticker: i for i, ticker in enumerate(tickers)}

    columns: dict[str, list] = {key: [] for key in COLUMN_KEYS}
    missing: list[int] = []
    for i, row in enumerate(rounded):
        ticker = row["ticker"]
        if ticker not in index_of:
            raise MetricsFormatError(f"row {i} is missing a ticker")
        close = row["close_price"]
        adj = row["etf_adj_close"]
        if close is not None and adj is None:
            stored_adj = None
            missing.append(i)
        elif close is not None and adj == close:
            stored_adj = None
        else:
            stored_adj = adj
        columns["ticker_ix"].append(index_of[ticker])
        columns["date"].append(row["date"])
        columns["close_price"].append(close)
        columns["nav"].append(row["nav"])
        columns["etf_adj_close"].append(stored_adj)
        columns["underlying_adj_close"].append(row["underlying_adj_close"])
        columns["aum"].append(row["aum"])
        columns["shares_outstanding"].append(row["shares_outstanding"])
        columns["shares_traded"].append(row["shares_traded"])
        columns["status"].append(row["status"])
        columns["stale"].append(row["stale"])
        columns["stale_kind"].append(row["stale_kind"])
        columns["stale_age_bdays"].append(row["stale_age_bdays"])
        columns["issuer_asof_date"].append(row["issuer_asof_date"])
        columns["market_asof_date"].append(row["market_asof_date"])
        columns["premium_discount_eligible"].append(row["premium_discount_eligible"])
        columns["premium_discount_status"].append(row["premium_discount_status"])

    out = {
        "format": FORMAT,
        "build_time": payload.get("build_time"),
        "public_history_days": payload.get("public_history_days"),
        "tickers": tickers,
        "columns": columns,
    }
    if missing:
        out["etf_adj_close_missing"] = missing
    return out


def dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def write_compact(path: Path) -> tuple[int, int]:
    """Rewrite ``path`` as canonical columnar JSON. Returns (before, after) bytes."""
    before = path.stat().st_size
    payload = json.loads(path.read_text(encoding="utf-8"))
    text = dumps(compact(payload)) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return before, path.stat().st_size


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default="data/etf_metrics_daily.json",
        type=Path,
        help="metrics JSON to rewrite in place (default: data/etf_metrics_daily.json)",
    )
    args = parser.parse_args()
    before, after = write_compact(args.path)
    print(
        f"{args.path}: {before} bytes ({before / 1024 / 1024:.3f} MiB) -> "
        f"{after} bytes ({after / 1024 / 1024:.3f} MiB)"
    )


if __name__ == "__main__":
    main()
