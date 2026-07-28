# DC Dashboard

Public, static ETF research dashboard for Diamond Creek.

This repository intentionally contains only:

- the browser UI (`index.html` and `assets/`);
- a bounded, sanitized public-data bundle (`data/`);
- validation and GitHub Pages deployment workflows.

Broker connectivity, account data, trading, portfolio construction, sizing,
locates, rebalancing, and proprietary algorithms live only in the private
`GoldmanDrew/Diamond-Creek-Execution` repository.

The public data bundle is produced in the private repository and copied here
through a one-way allowlisted export. This repository never checks out or
imports the private repository.

## Local preview

```bash
python -m http.server 8000
```

Open `http://127.0.0.1:8000/`.

## Validation

```bash
python scripts/validate_public_boundary.py
node --check assets/expected_decay.js
```

The dashboard is public. It does not contain a client-side password file or
account-specific Clear Street data.
