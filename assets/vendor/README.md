# Vendored browser runtime

| File | Version | Source |
|---|---|---|
| `react.production.min.js` | 18.2.0 | `https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js` |
| `react-dom.production.min.js` | 18.2.0 | `https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js` |

These were previously loaded from cdnjs at runtime, with no `integrity`
attribute. For a dashboard that gates access and drives trading decisions, that
made rendering depend on a third-party CDN being both reachable and unmodified.
Vendoring removes the network dependency and the supply-chain surface in one
step, and it is why the page now issues zero cross-origin script requests.

`babel-standalone` used to be loaded from the same CDN to compile 13k lines of
JSX in the browser on every page load. It is gone: `assets/app.jsx` is compiled
ahead of time by `build.mjs` into `assets/app.bundle.js`.

To upgrade, re-download both files at the new version, update the table above,
then run `node build.mjs` and confirm the dashboard renders.
