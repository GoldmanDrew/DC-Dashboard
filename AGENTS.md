# AGENTS.md — DC Dashboard

This is the public static-dashboard repository.

## Hard boundary

Allowed:

- `index.html` (shell + CSS only; the application lives in `assets/app.jsx`)
- public browser assets under `assets/`, including the vendored browser runtime
  in `assets/vendor/` and the prebuilt `assets/app.bundle.js`
- sanitized, bounded public datasets under `data/`
- public validation/deployment automation, `build.mjs`, `package.json`, `_headers`

Never add:

- execution, order, locate, or rebalance code;
- portfolio construction, sizing, solver, cadence, hedge, or ratchet code;
- `dcq/`, private config, broker clients, account reports, positions, NAV, P&L,
  trades, financing, or private risk snapshots;
- plaintext credentials, account identifiers, private keys, or `.env`;
- **any credential material or client-side login gate.** This repository is
  public, so everything in it is served by `raw.githubusercontent.com` regardless
  of the Cloudflare Access policy on the Worker and Pages hostnames. The former
  `data/investors.json` published the PBKDF2 hashes of both users while gating
  nothing — every data file behind the gate was equally readable. Access is the
  only gate; `validate_public_boundary.py` fails the build if a credential file
  or in-page gate reappears;
- a checkout, submodule, package import, or runtime dependency on
  `Diamond-Creek-Execution`.

The only bridge is the sanitized `data/public_bundle_manifest.json` export.

## Before every commit

```bash
python scripts/validate_public_boundary.py
node build.mjs --check
```

`assets/app.bundle.js` is committed so the repo stays deployable as plain static
files; `--check` is what stops it drifting from `assets/app.jsx`. Both CI
workflows run it too. Never re-inline JSX into `index.html`.
