# DC Dashboard

Public, static ETF research dashboard

This repository intentionally contains only:

- the browser UI (`index.html` shell + `assets/`, with the application in
  `assets/app.jsx` and its prebuilt `assets/app.bundle.js`);
- a bounded, sanitized public-data bundle (`data/`);
- validation and Cloudflare Pages deployment workflows.

The public data bundle is produced in the private repository and copied here
through a one-way allowlisted export. This repository never checks out or
imports the private repository.

## Build

`assets/app.jsx` is compiled ahead of time into `assets/app.bundle.js`:

```bash
npm install && npm run build
```

The application used to ship as a 13,000-line inline `<script type="text/babel">`
block that `babel-standalone` compiled in the browser on every page load, behind
a ~2.7 MB CDN download, before anything could render. Rebuild after any change to
`assets/app.jsx`; CI fails if the committed bundle does not match its source.

## Local preview

```bash
npm run serve
```

Open `http://127.0.0.1:8000/`.

## Validation

```bash
python scripts/validate_public_boundary.py
npm run check
npm test
```

## Access control

Access is enforced by **Cloudflare Access** in front of both
`app.diamond-creek-risk.workers.dev` and the Pages hostname. There is
deliberately no login in this repository: it is public, so any credential file
here is world-readable via `raw.githubusercontent.com`, and a client-side gate
over public files gates nothing. Account-specific Clear Street data is never
published here.

## Data caching

Data files are fetched as `data/<name>.json?v=<sha256 prefix>` from
`data/public_bundle_manifest.json`, so a URL changes only when its content does.
`_headers` marks them immutable. A revisit costs one 4 KB manifest fetch instead
of re-downloading and re-parsing ~54 MB of JSON.
