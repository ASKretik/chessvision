/* Frontend smoke test — runs the BUILT bundle under jsdom against a live
 * backend on 127.0.0.1:8000.
 *
 * Not a browser: it cannot judge layout, paint or CSS. What it does catch is
 * the class of bug that a typecheck cannot and that looks fine in a
 * screenshot — a crash on mount, and a response field the UI reads under the
 * wrong name. It has already caught three:
 *
 *   - Comment.text did not exist; the real field is `comment`, so every
 *     comment card rendered an empty paragraph. The type checker was happy,
 *     because the hand-written interface was simply wrong.
 *   - The clock was blank at the start of every game.
 *   - Nine comments rendered as six: a row with two commented moves kept
 *     only the first.
 *
 *   npm run build && npm run smoke
 */
import { JSDOM, VirtualConsole } from 'jsdom';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const BASE = 'http://127.0.0.1:8000';
const STATIC = join(process.cwd(), '..', 'static');
let route = process.argv[2] ?? '/';
const waitMs = Number(process.argv[3] ?? 4000);

/* `new` starts a real analysis and opens it IMMEDIATELY, so the page is
   mounted while the job is still running and renders the intermediate
   `analysis` frame. Opening a finished job cannot test that, which is how a
   crash on that frame survived a clean smoke run. */
if (route === 'new') {
  // Carries %clk and Elo headers on purpose: without them the time panel
  // and the performance rating both correctly render "not available", and
  // the smoke run would pass while testing neither.
  const pgn =
    '[Event "smoke"]\n[White "a"]\n[Black "b"]\n[Result "0-1"]\n' +
    '[WhiteElo "1402"]\n[BlackElo "1372"]\n[TimeControl "300+0"]\n\n' +
    '1. e4 { [%clk 0:05:00] } e5 { [%clk 0:05:00] } ' +
    '2. Nf3 { [%clk 0:04:55] } Nc6 { [%clk 0:04:56] } ' +
    '3. Bc4 { [%clk 0:04:50] } Nf6 { [%clk 0:04:50] } ' +
    '4. Ng5 { [%clk 0:04:10] } d5 { [%clk 0:04:42] } ' +
    '5. exd5 { [%clk 0:04:05] } Nxd5 { [%clk 0:04:30] } ' +
    '6. Nxf7 { [%clk 0:03:55] } Kxf7 { [%clk 0:04:25] } ' +
    '7. Qf3+ { [%clk 0:03:45] } Ke6 { [%clk 0:03:20] } ' +
    '8. Nc3 { [%clk 0:03:35] } Nce7 { [%clk 0:02:30] } ' +
    '9. O-O { [%clk 0:03:25] } c6 { [%clk 0:02:20] } ' +
    '10. d4 { [%clk 0:03:15] } Kd7 { [%clk 0:01:40] } ' +
    '11. dxe5 { [%clk 0:03:05] } Qb6 { [%clk 0:01:00] } 0-1\n';
  const r = await fetch(BASE + '/analyse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pgn, commentary: true }),
  });
  const job = await r.json();
  route = '/analyse/' + job.job_id;
  console.log('started a live job:', job.job_id);
}

const html = await (await fetch(BASE + route)).text();

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.map(String).join(' ')));

console.log('html fetched:', html.length, 'bytes');
const dom = new JSDOM(html, {
  url: BASE + route,
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  virtualConsole: vc,
});
const { window } = dom;

window.fetch = (input, init) => {
  const url = typeof input === 'string' && input.startsWith('/') ? BASE + input : input;
  return fetch(url, init);
};
/* A REAL EventSource over fetch streaming.
 *
 * jsdom has none, and the obvious stub — one that reports CLOSED so the hook
 * falls back to polling — is worse than useless: polling only ever sees the
 * FINISHED job, so the intermediate `analysis` frame is never rendered. That
 * frame carries Part A's report without Part B's commentary, and reading
 * `report.commentary.comments` on it crashed the page to blank on every
 * single analysis. The stub reported a clean run throughout.
 *
 * So this parses the real stream, and `--poll` opts into the old behaviour
 * when the polling path is what is under test. */
class RealEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  constructor(url) {
    this.readyState = 0;
    this.onerror = null;
    this.listeners = new Map();
    this.controller = new AbortController();
    this.#run(url);
  }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(fn);
  }
  close() {
    this.readyState = 2;
    this.controller.abort();
  }
  #emit(name, data) {
    for (const fn of this.listeners.get(name) ?? []) {
      try { fn({ data }); } catch (e) { errors.push('handler: ' + (e.stack || e.message)); }
    }
  }
  async #run(url) {
    try {
      const res = await fetch(url.startsWith('/') ? BASE + url : url,
                              { signal: this.controller.signal });
      // A non-OK response is not a fetch rejection, so without this the
      // stream just ended silently and the page sat on "queued" forever.
      // That is exactly how a 404 on the events endpoint -- every signed-in
      // user's own analysis, once jobs had owners -- went unnoticed.
      if (!res.ok) {
        this.readyState = 2;
        errors.push(`EventSource: HTTP ${res.status} on ${url}`);
        this.onerror && this.onerror(new Event('error'));
        return;
      }
      this.readyState = 1;
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        // Frames are separated by a blank line.
        let i;
        while ((i = buf.indexOf('\n\n')) !== -1) {
          const frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          let name = 'message';
          let data = '';
          for (const line of frame.split('\n')) {
            if (line.startsWith('event: ')) name = line.slice(7).trim();
            else if (line.startsWith('data: ')) data += line.slice(6);
          }
          if (data) {
            seenEvents.push(name);
            this.#emit(name, data);
          }
        }
      }
      this.readyState = 2;
    } catch (e) {
      if (e.name === 'AbortError') return;
      this.readyState = 2;
      errors.push('EventSource: ' + e.message);
      this.onerror && this.onerror(new Event('error'));
    }
  }
}

const seenEvents = [];
window.EventSource = process.argv.includes('--poll')
  ? class {
      static CLOSED = 2;
      constructor() {
        this.readyState = 2;
        this.onerror = null;
        setTimeout(() => this.onerror && this.onerror(new Event('error')), 0);
      }
      addEventListener() {}
      close() {}
    }
  : RealEventSource;
window.scrollTo = () => {};

/* An optional session, so the signed-in half of the app can be rendered.
   `--as user:password` signs in first and seeds localStorage exactly as the
   AuthProvider would, which is the only way to see a tab that is gated on
   being signed in. */
const asArg = process.argv.find((a) => a.startsWith('--as='));
if (asArg) {
  const [u, p] = asArg.slice(5).split(':');
  const res = await fetch(BASE + '/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: u, password: p }),
  });
  if (res.ok) {
    const { token } = await res.json();
    window.localStorage.setItem('chess.session', token);
    console.log('signed in as', u);
  } else {
    console.log('sign-in failed:', res.status);
  }
}
window.Element.prototype.scrollIntoView ||= function () {};
window.navigator.clipboard ||= { writeText: async () => {} };

const bundle = html.match(/src="([^"]+\.js)"/)[1];
try {
  window.eval(readFileSync(join(STATIC, bundle.replace(/^\//, '')), 'utf8'));
} catch (e) {
  errors.push('bundle threw: ' + (e.stack || e.message));
}

console.log('bundle evaluated, waiting', waitMs, 'ms for effects...');
await new Promise((r) => setTimeout(r, waitMs));

const d = window.document;
const root = d.getElementById('root');
const text = (root?.textContent ?? '').replace(/\s+/g, ' ').trim();

console.log('=== route:', route);
console.log('root children :', root?.children.length ?? 0);
console.log('boards        :', d.querySelectorAll('.board').length);
console.log('squares       :', d.querySelectorAll('.sq').length);
console.log('pieces        :', d.querySelectorAll('.piece').length);
console.log('eval bar      :', d.querySelectorAll('.evalbar').length);
console.log('eval graph    :', d.querySelectorAll('.evalgraph').length);
console.log('move cells    :', d.querySelectorAll('.ml-move').length);
console.log('comments      :', d.querySelectorAll('.ml-comment').length);
const pick = (sel) => (d.querySelector(sel)?.textContent ?? '').replace(/\s+/g,' ').trim();
console.log('eval header   :', pick('.gv-evalhead'));
console.log('player top    :', pick('.gv-board-col .pstrip'));
console.log('clocks shown  :', d.querySelectorAll('.pstrip-clock').length);
console.log('first comment :', pick('.ml-comment').slice(0, 320));
console.log('faults in list:', d.querySelectorAll('.ml-move.is-blunder, .ml-move.is-mistake, .ml-move.is-inaccuracy').length);
console.log('time panel    :', d.querySelectorAll('.tp').length);
console.log('time rows     :', d.querySelectorAll('.tp-row').length);
console.log('perf rating   :', /performance rating/.test(text) ? 'shown' : 'absent');
console.log('time lead     :', pick('.tp-lead').slice(0, 180));
console.log('graph dots    :', d.querySelectorAll('.eg-dot').length);
console.log('text          :', text.slice(0, 220));
console.log('sse frames    :', seenEvents.length
  ? [...new Set(seenEvents)].join(', ') + ` (${seenEvents.length})`
  : '(none — polling path)');
console.log(errors.length ? '--- ERRORS ---' : '--- no errors ---');
for (const e of errors.slice(0, 5)) console.log(e.slice(0, 400));

// jsdom holds timers and sockets open; nothing below here matters.
dom.window.close();
process.exit(0);
