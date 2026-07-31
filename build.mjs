/**
 * Build assets/app.jsx -> assets/app.bundle.js with esbuild.
 *
 * The application used to ship as a 13,000-line inline `<script type="text/babel">`
 * block, transpiled in the browser by babel-standalone on every page load. That
 * cost a ~2.7MB CDN download plus a full JSX compile before first paint, on every
 * visit, for output that never varies between visits.
 *
 * `--check` verifies the committed bundle matches the current source, so the two
 * cannot drift. CI runs that; humans run the plain build.
 *
 *   node build.mjs           build (writes the bundle)
 *   node build.mjs --check   fail if the committed bundle is stale
 */
import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';

const ENTRY = 'assets/app.jsx';
const OUT = 'assets/app.bundle.js';
const check = process.argv.includes('--check');

// React/ReactDOM and the assets/*.js helpers are plain <script> globals, not
// modules. `format: 'iife'` with no imports in the entry keeps them resolving at
// runtime exactly as they did when the code was inline.
const result = await build({
  entryPoints: [ENTRY],
  outfile: OUT,
  bundle: false,
  write: false,
  format: 'iife',
  target: ['es2020'],
  loader: { '.jsx': 'jsx' },
  jsx: 'transform',
  minify: true,
  legalComments: 'none',
  logLevel: 'warning',
});

const built = result.outputFiles[0].text;
const digest = (s) => createHash('sha256').update(s).digest('hex').slice(0, 12);

if (check) {
  let committed;
  try {
    committed = await readFile(OUT, 'utf8');
  } catch {
    console.error(`[build] ${OUT} is missing. Run: node build.mjs`);
    process.exit(1);
  }
  if (committed !== built) {
    console.error(
      `[build] ${OUT} is stale.\n` +
        `        committed sha256=${digest(committed)}\n` +
        `        rebuilt   sha256=${digest(built)}\n` +
        `        Run: node build.mjs`
    );
    process.exit(1);
  }
  console.log(`[build] ${OUT} matches ${ENTRY} (sha256=${digest(built)})`);
} else {
  await writeFile(OUT, built, 'utf8');
  const src = await readFile(ENTRY, 'utf8');
  const kb = (n) => `${(n / 1024).toFixed(0)}KB`;
  console.log(
    `[build] ${ENTRY} ${kb(src.length)} -> ${OUT} ${kb(built.length)} ` +
      `(sha256=${digest(built)})`
  );
}
