# DC Dashboard

Public, static ETF research dashboard

This repository intentionally contains only:

- the browser UI (`index.html` and `assets/`);
- a bounded, sanitized public-data bundle (`data/`);
- validation and GitHub Pages deployment workflows.

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

The dashboard presents a login screen
Passwords are checked against PBKDF2 hashes in plaintext
passwords and account-specific Clear Street data are not stored here. Because
this is a static public site, the login is a user-interface gate rather than
server-enforced access control.
