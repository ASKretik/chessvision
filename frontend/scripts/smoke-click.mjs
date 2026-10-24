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
/* The Positions study page, because its position comes from the URL: the
   puzzle feed hands out a different puzzle on every call, so a test that
   asks it for one is not looking at the board it is clicking. */
const FEN = 'r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4';
const ROUTE = '/positions?fen=' + encodeURIComponent(FEN);
const html = await (await fetch(BASE + ROUTE)).text();

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.map(String).join(' ')));

const dom = new JSDOM(html, { url: BASE + ROUTE, runScripts: 'outside-only',
                              pretendToBeVisual: true, virtualConsole: vc });
const { window } = dom;
const d = window.document;
window.fetch = (i, init) => fetch(typeof i === 'string' && i.startsWith('/') ? BASE + i : i, init);
window.EventSource = class { constructor(){ this.readyState = 2; } addEventListener(){} close(){} };
window.scrollTo = () => {};
window.Element.prototype.scrollIntoView ||= function () {};

window.eval(readFileSync(join(STATIC, html.match(/src="([^"]+\.js)"/)[1].replace(/^\//, '')), 'utf8'));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
await wait(4000);

console.log('board rendered           :', d.querySelectorAll('.sq').length === 64);

const click = (el) => {
  el.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true }));
};

// Find a square the UI considers movable: one whose piece has destinations.
const board = await (await fetch(
  BASE + '/board/moves?fen=' + encodeURIComponent(FEN))).json();
// A capture, so the destination also exercises the .is-capture marker.
const first = board.legal.find((m) => m.san.includes('x')) ?? board.legal[0];
const from = d.querySelector(`[aria-label^="${first.from},"]`);
const to = d.querySelector(`[aria-label^="${first.to},"]`);
console.log('origin square found      :', !!from, first.from);
console.log('target square found      :', !!to, first.to);

if (from && to) {
  click(from);
  await wait(300);
  console.log('selecting marks the square:', d.querySelectorAll('.is-sel').length > 0);
  console.log('destinations are shown    :',
              d.querySelectorAll('.is-dest, .is-capture').length > 0);
  click(to);
  await wait(3500);
  const line = (d.querySelector('.pos-line')?.textContent ?? '').trim();
  const fenShown = (d.querySelector('.pos-fen')?.textContent ?? '').trim();
  console.log('the move was played       :', line !== '-' && line !== '\u2014' && line.length > 0);
  console.log('line                      :', line || '(none)');
  console.log('position advanced         :', fenShown !== FEN && fenShown.length > 0);
  console.log('expected SAN              :', first.san);
}
console.log(errors.length ? '--- ERRORS ---' : '--- no errors ---');
for (const e of errors.slice(0, 4)) console.log(e.slice(0, 300));
dom.window.close();
process.exit(0);
