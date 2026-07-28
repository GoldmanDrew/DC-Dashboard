# AGENTS.md — DC Dashboard

This is the public static-dashboard repository.

## Hard boundary

Allowed:

- `index.html`
- public browser assets under `assets/`
- sanitized, bounded public datasets under `data/`
- public validation/deployment automation

Never add:

- execution, order, locate, or rebalance code;
- portfolio construction, sizing, solver, cadence, hedge, or ratchet code;
- `dcq/`, private config, broker clients, account reports, positions, NAV, P&L,
  trades, financing, or private risk snapshots;
- credentials, password hashes, account identifiers, private keys, or `.env`;
- a checkout, submodule, package import, or runtime dependency on
  `Diamond-Creek-Execution`.

The only bridge is the sanitized `data/public_bundle_manifest.json` export.
Run `python scripts/validate_public_boundary.py` before every commit.
