/*
 * E.4 board component tests, headless.
 *
 * The board is the one piece of frontend with real logic in it, and it had
 * two bugs that no Python test could see: a duplicate `const` that killed
 * the whole script, and a swallowed fetch failure that left a board drawn
 * but dead. So the class is extracted from harness/index.html and driven
 * against a stub DOM.
 *
 *   macOS: /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/\
 *            Helpers/jsc tests/test_board_ui.js
 *   node:  node tests/test_board_ui.js
 *
 * Parsing the page at all is half the value: a syntax error anywhere in the
 * harness fails this test.
 */

var IS_NODE = typeof process !== 'undefined' && process.versions && process.versions.node;
var print = IS_NODE ? console.log : print;
var ROOT = IS_NODE ? require('path').join(__dirname, '..') : '..';

/* harness/index.html, not static/ -- static/ is Vite's output directory and
   is emptied on every build, so the harness moved out of it. */
function readPage() {
  if (IS_NODE) return require('fs').readFileSync(ROOT + '/harness/index.html', 'utf8');
  return readFile('../harness/index.html');
}

var results = [];
function check(name, cond, detail) { results.push([name, !!cond, detail || '']); }

/* ---- stub DOM: className and classList are the same thing, as in a browser ---- */
function El(tag) {
  this.tagName = tag; this.children = []; this.dataset = {};
  this.textContent = ''; this._innerHTML = ''; this.onclick = null;
  this.parentElement = null; this.title = '';
}
El.prototype = {
  get innerHTML() { return this._innerHTML; },
  set innerHTML(v) { this._innerHTML = v; if (v === '') this.children = []; },
  appendChild: function (c) {
    var k = this.children.indexOf(c);
    if (k >= 0) this.children.splice(k, 1);
    this.children.push(c); c.parentElement = this; return c;
  },
  remove: function () {
    if (!this.parentElement) return;
    var k = this.parentElement.children.indexOf(this);
    if (k >= 0) this.parentElement.children.splice(k, 1);
  },
  querySelector: function (sel) {
    var m = /\[data-sq="([^"]+)"\]/.exec(sel);
    for (var i = 0; i < this.children.length; i++) {
      var c = this.children[i];
      if (m ? c.dataset.sq === m[1] : c.classList.contains(sel.replace('.', ''))) return c;
    }
    return null;
  },
  querySelectorAll: function () { return []; },
  byClass: function (c) {
    return this.children.filter(function (x) { return x.classList.contains(c); });
  }
};
Object.defineProperty(El.prototype, 'classList', {
  get: function () {
    if (!this._cl) this._cl = {
      _s: {},
      add: function (c) { this._s[c] = true; },
      remove: function (c) { delete this._s[c]; },
      contains: function (c) { return !!this._s[c]; },
      toggle: function (c, on) { if (on) this.add(c); else this.remove(c); },
      list: function () { return Object.keys(this._s); }
    };
    return this._cl;
  }
});
Object.defineProperty(El.prototype, 'className', {
  get: function () { return this.classList.list().join(' '); },
  set: function (v) {
    var cl = this.classList;
    cl.list().forEach(function (c) { cl.remove(c); });
    String(v).split(/\s+/).filter(Boolean).forEach(function (c) { cl.add(c); });
  }
});

var document = { createElement: function (t) { return new El(t); } };
var console_ = typeof console !== 'undefined' ? console : {error: function(){}, log: function(){}};
if (typeof console === 'undefined') { console = console_; }

/* ---- the class under test, lifted out of the page ---- */
var page = readPage();
var js = page.split('<script>')[1].split('</script>')[0];
check('the harness page contains one script block',
      page.split('<script>').length === 2);
var from = js.indexOf('const PIECES'), to = js.indexOf('aBoard = new Board');
check('the Board class is where the tests expect it', from > -1 && to > from);
var source = js.slice(from, to);
check('the page defines PIECES exactly once',
      (js.match(/const PIECES/g) || []).length === 1,
      'a duplicate const is a SyntaxError that kills the whole page');

var Board;
try {
  Board = new Function('document', 'fetch', 'console',
                       source + '; return Board;')(document, mkFetch, console_);
  check('the whole page parses as JavaScript', true);
} catch (e) {
  check('the whole page parses as JavaScript', false, String(e));
}

/* fetch is swapped per-case through this holder */
var MODE = 'ok', LEGAL = null;
function mkFetch(url) {
  if (MODE === 'reject') return Promise.reject(new Error('NetworkError'));
  if (MODE === '422') return Promise.resolve({
    ok: false, status: 422,
    json: function () { return Promise.resolve({detail: 'Invalid FEN'}); }});
  return Promise.resolve({ok: true, status: 200,
    json: function () { return Promise.resolve(LEGAL); }});
}

/* Two real positions, from the server's own output shape. */
var START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
var OPENING = {
  turn: 'white', in_check: false, is_game_over: false, outcome: null,
  legal: [
    {uci: 'e2e4', san: 'e4', from: 'e2', to: 'e4', promotion: null,
     fen_after: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1',
     is_check: false, is_checkmate: false},
    {uci: 'e2e3', san: 'e3', from: 'e2', to: 'e3', promotion: null,
     fen_after: 'rnbqkbnr/pppppppp/8/8/8/4P3/PPPP1PPP/RNBQKBNR b KQkq - 0 1',
     is_check: false, is_checkmate: false},
    {uci: 'g1f3', san: 'Nf3', from: 'g1', to: 'f3', promotion: null,
     fen_after: 'rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1',
     is_check: false, is_checkmate: false}
  ]
};
var PROMO_FEN = '8/P6k/8/8/8/8/7K/8 w - - 0 1';
var PROMO = {
  turn: 'white', in_check: false, is_game_over: false, outcome: null,
  legal: ['queen', 'rook', 'bishop', 'knight'].map(function (p) {
    return {uci: 'a7a8' + p[0], san: 'a8=' + p[0].toUpperCase(), from: 'a7',
            to: 'a8', promotion: p, fen_after: '8/7k/8/8/8/8/7K/8 b - - 0 1',
            is_check: false, is_checkmate: false};
  })
};

function newBoard(opts) {
  var host = new El('div'), wrap = new El('div');
  wrap.appendChild(host);
  var played = [];
  opts = opts || {};
  opts.onMove = function (m) { played.push(m.san); };
  var b = new Board(host, opts);
  return {b: b, host: host, wrap: wrap, played: played};
}

MODE = 'ok'; LEGAL = OPENING;
var t = newBoard({interactive: true});

t.b.set(START).then(function () {
  check('a board draws 64 squares', t.host.children.length === 64,
        String(t.host.children.length));
  check('legal moves come from the endpoint', t.b.legal.length === 3,
        String(t.b.legal.length));
  check('squares with a move are marked movable',
        t.host.byClass('movable').length === 2,
        'e2 and g1: ' + t.host.byClass('movable').length);
  check('every square takes a click when interactive',
        t.host.children.filter(function (c) { return typeof c.onclick === 'function'; })
          .length === 64);
  check('no error is reported on a good load', t.b.error === null, String(t.b.error));

  var e2 = t.host.querySelector('[data-sq="e2"]');
  check('the piece square exists and is movable',
        e2 && e2.classList.contains('movable'));
  e2.onclick();
  check('clicking a piece selects it', t.b.sel === 'e2', String(t.b.sel));
  check('its destinations are highlighted', t.host.byClass('dest').length === 2,
        String(t.host.byClass('dest').length));

  var e4 = t.host.querySelector('[data-sq="e4"]');
  e4.onclick();
  check('clicking a destination plays the move',
        t.played.length === 1 && t.played[0] === 'e4', JSON.stringify(t.played));
  check('the board moves to the resulting position',
        t.b.fen === OPENING.legal[0].fen_after, t.b.fen);
  check('the move is highlighted as the last move',
        t.b.lastMove && t.b.lastMove.from === 'e2' && t.b.lastMove.to === 'e4');

  // Selection is a toggle: clicking the same piece again puts the dots away.
  LEGAL = OPENING;
  return t.b.set(START);
}).then(function () {
  var e2 = t.host.querySelector('[data-sq="e2"]');
  e2.onclick();
  check('a first click selects', t.b.sel === 'e2', String(t.b.sel));
  check('and shows its destinations', t.host.byClass('dest').length === 2,
        String(t.host.byClass('dest').length));
  var playedBefore = t.played.length;
  e2.onclick();
  check('a second click on the same piece deselects it',
        t.b.sel === null, String(t.b.sel));
  check('and the destination dots vanish', t.host.byClass('dest').length === 0,
        String(t.host.byClass('dest').length));
  check('the square is no longer marked selected',
        !t.host.querySelector('[data-sq="e2"]').classList.contains('sel-sq'));
  check('toggling off plays no move', t.played.length === playedBefore,
        JSON.stringify(t.played));
  e2.onclick();
  check('a third click selects again', t.b.sel === 'e2', String(t.b.sel));
  // switching directly to another piece still works, rather than toggling off
  t.host.querySelector('[data-sq="g1"]').onclick();
  check('clicking a different piece switches the selection',
        t.b.sel === 'g1', String(t.b.sel));
  check('and shows that piece\'s destinations',
        t.host.byClass('dest').length === 1,
        String(t.host.byClass('dest').length));

  // a square with no moves clears the selection rather than doing nothing odd
  return t.b.set(START);
}).then(function () {
  t.host.querySelector('[data-sq="e2"]').onclick();
  t.host.querySelector('[data-sq="a1"]').onclick();
  check('clicking an empty-ish square clears the selection',
        t.b.sel === null, String(t.b.sel));

  // a locked board ignores clicks -- the puzzle tab relies on this while
  // it waits for the server
  t.b.locked = true;
  var before = t.played.length;
  t.host.querySelector('[data-sq="e2"]').onclick();
  check('a locked board ignores clicks',
        t.b.sel === null && t.played.length === before);
  t.b.locked = false;

  // flipping must not break the mapping between a square and its handler
  var f = newBoard({interactive: true, flipped: true});
  LEGAL = OPENING;
  return f.b.set(START).then(function () {
    check('a flipped board still draws 64 squares', f.host.children.length === 64);
    // Reading order reversed: with black at the bottom the top-left square
    // is h1, not a1.
    check('a flipped board puts h1 top-left',
          f.host.children[0].dataset.sq === 'h1',
          f.host.children[0].dataset.sq);
    check('and a8 bottom-right',
          f.host.children[63].dataset.sq === 'a8',
          f.host.children[63].dataset.sq);
    f.host.querySelector('[data-sq="e2"]').onclick();
    check('a flipped board still selects the right square', f.b.sel === 'e2',
          String(f.b.sel));
  });
}).then(function () {
  // promotion: four moves sharing from+to must raise a picker, not play one
  var p = newBoard({interactive: true});
  LEGAL = PROMO;
  return p.b.set(PROMO_FEN).then(function () {
    p.host.querySelector('[data-sq="a7"]').onclick();
    p.host.querySelector('[data-sq="a8"]').onclick();
    check('a promotion does not silently pick a piece', p.played.length === 0,
          JSON.stringify(p.played));
    check('a promotion raises a picker', p.b.pending && p.b.pending.length === 4,
          String(p.b.pending && p.b.pending.length));
    check('the picker is attached beside the board',
          p.wrap.byClass('promo').length === 1);
  });
}).then(function () {
  // the failure modes that previously produced a dead board in silence
  var f = newBoard({interactive: true});
  MODE = 'reject';
  return f.b.set(START).then(function () {
    check('a network failure is reported, not swallowed',
          /NetworkError/.test(f.b.error || ''), String(f.b.error));
    check('and it is shown next to the board',
          f.wrap.byClass('boarderr').length === 1);
    MODE = '422';
    return f.b.set(START);
  }).then(function () {
    check('a 422 is reported with the server\'s reason',
          /Invalid FEN/.test(f.b.error || ''), String(f.b.error));
    MODE = 'ok'; LEGAL = {legal: [], is_game_over: false, turn: 'white'};
    return f.b.set(START);
  }).then(function () {
    check('an empty move list is reported as not live',
          /no legal moves/.test(f.b.error || ''), String(f.b.error));
    MODE = 'ok';
    LEGAL = {legal: [], is_game_over: true, outcome: '1-0', turn: 'white'};
    return f.b.set(START);
  }).then(function () {
    check('but a finished game is not an error', f.b.error === null,
          String(f.b.error));
    check('the pieces are still drawn when the board is not live',
          f.host.children.length === 64);
  });
}).then(report, function (e) {
  check('the suite ran to completion', false, String(e && e.stack || e));
  report();
});

function report() {
  var width = 0;
  results.forEach(function (r) { width = Math.max(width, r[0].length); });
  print('');
  results.forEach(function (r) {
    var line = '  [' + (r[1] ? 'PASS' : 'FAIL') + '] ' + r[0];
    while (line.length < width + 9) line += ' ';
    if (r[2] && !r[1]) line += '   <- ' + r[2];
    print(line);
  });
  var passed = results.filter(function (r) { return r[1]; }).length;
  print('');
  print(passed + '/' + results.length + ' checks passed');
  if (IS_NODE && passed !== results.length) process.exit(1);
}
