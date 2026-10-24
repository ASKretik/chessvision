import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The backend's own paths. Everything NOT listed here is an SPA route and is
// served by Vite in dev / index.html in production.
//
// Two of these collide with SPA routes of the same name and are resolved by
// method or specificity, not by path:
//   /analyse       GET -> the SPA page,  POST -> the API
//   /puzzles       GET -> the SPA page,  GET /puzzles/next -> the API
// The endpoints this spec adds (positions, play) live under /api to avoid the
// same collision, because there GET would be ambiguous. See the spec doc.
const API_PATHS = [
  '/config',
  '/health',
  '/ready',
  '/jobs',
  '/me',
  // The move source every board reads. Without this the dev server answers
  // /board/moves with index.html and no board has any legal moves.
  '/board',
  '/ops',
  // The no-framework dev page, which must keep working independently of the
  // SPA build.
  '/harness',
  '/api',
  '/openapi.json',
  '/docs',
  '/redoc',
];

const BACKEND = process.env.VITE_DEV_BACKEND ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  // '/' when partE.py serves the SPA; '/<repo>/' for the GitHub Pages build.
  base: process.env.VITE_BASE ?? '/',
  build: {
    // partE.py serves this directory. Keep it next to the backend module.
    // The Pages workflow overrides it with --outDir.
    outDir: '../static',
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      ...Object.fromEntries(
        API_PATHS.map((p) => [p, { target: BACKEND, changeOrigin: true }]),
      ),
      // POST /analyse must reach the backend while GET /analyse stays the SPA.
      '/analyse': {
        target: BACKEND,
        changeOrigin: true,
        bypass: (req) => (req.method === 'GET' ? '/index.html' : undefined),
      },
      // /puzzles/next, /puzzles/{id}/step and /puzzles/{id}/attempt are API;
      // bare /puzzles is the page. `step` is not optional -- it is how an
      // interactive board gets the opponent's reply, so leaving it out makes
      // every puzzle unplayable in dev while looking like a frontend bug.
      '/puzzles': {
        target: BACKEND,
        changeOrigin: true,
        bypass: (req) => {
          const url = req.url ?? '';
          const isApi =
            url.startsWith('/puzzles/next') ||
            /\/puzzles\/[^/]+\/(attempt|step)\b/.test(url);
          return isApi ? undefined : '/index.html';
        },
      },
    },
  },
});
