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

Before publishing a new bundle, also run the health check — it catches what the
boundary validator structurally cannot (see below):

```bash
python scripts/public_health_sentinel.py check --baseline HEAD
```

## Two different questions

`validate_public_boundary.py` asks **"is this bundle allowed to be public, and
internally consistent?"** — forbidden paths, credential material, sanitization,
and manifest sha256/byte agreement. It is a pre-commit gate and runs on push.

`public_health_sentinel.py` asks **"is the published bundle still alive and
still sane?"** Those are different failure modes, and the boundary gate cannot
see any of them:

- **the bundle stops arriving.** Publishing runs locally in the private repo, so
  a machine that stops publishing leaves this site serving stale data forever.
  Nothing here was scheduled, so nothing noticed. `.github/workflows/public-health.yml`
  is the watchdog (3x weekdays + one weekend check).
- **a bare `NaN`/`Infinity` token.** Python's `json.loads` accepts them; the
  browser's `JSON.parse` does not. Producer-side "valid" is not consumer-side
  parseable — this exact combination once blanked a whole dashboard.
- **a bundle that is consistent but wrong.** The manifest is regenerated from
  disk at publish time, so it proves "what shipped is what we meant to ship",
  never "what shipped is correct". A bundle with 50 records instead of 545
  passes every hash check; only comparison against the *previous* published
  bundle catches it.
- **a file published outside the manifest.** The validator checks
  listed→present; the reverse direction is the leak-adjacent one.

The sentinel **never writes into this repository**: a commit here triggers a
Cloudflare Pages deploy, and any file under `data/` would be swept into the
bundle manifest. Its report goes to a path outside the repo, and findings reach
you through the job summary and one rolling `ops/public-health` issue that
auto-closes on recovery.

Tests are stdlib `unittest` (there is no `requirements.txt` — it is a forbidden
file), run per-file like the Node suite:

```bash
python tests/test_public_health_sentinel.py
```

`assets/app.bundle.js` is committed so the repo stays deployable as plain static
files; `--check` is what stops it drifting from `assets/app.jsx`. Both CI
workflows run it too. Never re-inline JSX into `index.html`.
