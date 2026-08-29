// A minimal DOM/window stub, enough to run client/replay_broadcast.html's own
// game block against a real replay document under node.
//
// The sandbox has no browser and CI's wasm-viewer job is the real gate, but a
// stub run catches the failure that gate cannot report cheaply: a throw in the
// boot path, a scorebug that never builds, a scrubber with no beats, or
// playback that does not advance. static_replay.js (the wasm half) is stubbed;
// EVERYTHING else -- the pinned chrome_common.js and the page's own block --
// is the shipped code.
//
//   node tests/page_dom_harness.mjs <repo-root> <replay.json>
//
// Prints one JSON line and exits non-zero on any failure.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const [root, replayPath] = process.argv.slice(2);
if (!root || !replayPath) {
  console.error('usage: page_dom_harness.mjs <repo-root> <replay>');
  process.exit(2);
}

const nodes = new Map();
function makeNode(id) {
  const node = {
    id,
    tag: '',
    children: [],
    childNodes: [],
    attrs: {},
    style: { setProperty() {}, getPropertyValue() { return ''; } },
    classList: {
      set: new Set(),
      add(c) { this.set.add(c); },
      remove(c) { this.set.delete(c); },
      contains(c) { return this.set.has(c); },
      toggle(c, on) {
        if (on === undefined) { this.set.has(c) ? this.set.delete(c) : this.set.add(c); }
        else if (on) { this.set.add(c); } else { this.set.delete(c); }
      },
    },
    listeners: {},
    text: '',
    html: '',
    queried: new Map(),
    get textContent() { return this.text; },
    set textContent(v) { this.text = String(v); },
    get innerText() { return this.text; },
    get innerHTML() { return this.html; },
    set innerHTML(v) { this.html = String(v); this.children = []; this.childNodes = []; },
    appendChild(child) { this.children.push(child); this.childNodes.push(child); return child; },
    removeChild(child) {
      const i = this.children.indexOf(child);
      if (i >= 0) { this.children.splice(i, 1); this.childNodes.splice(i, 1); }
    },
    get firstChild() { return this.childNodes[0]; },
    querySelector(sel) {
      if (!this.queried.has(sel)) this.queried.set(sel, makeNode(sel));
      return this.queried.get(sel);
    },
    querySelectorAll() { return []; },
    addEventListener(kind, fn) { (this.listeners[kind] ||= []).push(fn); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    getBoundingClientRect() {
      return { width: 1235, height: 659, left: 0, top: 0, bottom: 0, right: 0 };
    },
    clientWidth: 1235,
    clientHeight: 659,
  };
  return node;
}

const html = makeNode('html');
const documentListeners = {};
globalThis.document = {
  documentElement: html,
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  },
  createElement(tag) { const n = makeNode('<' + tag + '>'); n.tag = tag; return n; },
  createElementNS(_ns, tag) { const n = makeNode('<' + tag + '>'); n.tag = tag; return n; },
  querySelector() { return null; },
  addEventListener(kind, fn) { (documentListeners[kind] ||= []).push(fn); },
  defaultView: { getComputedStyle() { return { fontSize: '12px', fontFamily: 'sans-serif' }; } },
};
globalThis.addEventListener = () => {};
globalThis.window = globalThis;
globalThis.location = { search: '?replay=/fixture.replay', protocol: 'http:', host: 'x', pathname: '/index.html' };
globalThis.devicePixelRatio = 1;

// rAF is driven by hand below, so playback advances deterministically.
const frames = [];
globalThis.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
globalThis.MutationObserver = class { constructor(fn) { this.fn = fn; } observe() { mutation.push(this.fn); } };
const mutation = [];

const bytes = readFileSync(replayPath);
globalThis.fetch = async () => ({
  ok: true,
  arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
});

const commands = [];
let started = -1;
globalThis.HaliteStaticReplay = {
  createCore() {
    return {
      start(replayBytes) {
        started = replayBytes ? replayBytes.byteLength : -1;
        html.setAttribute('data-replay-loaded', 'true');
        mutation.forEach((fn) => fn());
      },
      sendCommand(text) { commands.push(text); },
      setViewportFit() {},
      getTransform() { return {}; },
      stop() {},
    };
  },
};

function fail(message) { console.error('FAIL: ' + message); process.exit(1); }

// The pinned chrome_common.js, then the page's own block.
const page = readFileSync(join(root, 'client/replay_broadcast.html'), 'utf8');
const block = page.match(/<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/)[1];
(0, eval)(readFileSync(join(root, 'client/chrome_common.js'), 'utf8'));
(0, eval)(block);

await new Promise((resolve) => setTimeout(resolve, 300));

const error = html.getAttribute('data-replay-error');
if (error) fail('the page set data-replay-error: ' + error);
if (html.getAttribute('data-replay-loaded') !== 'true') fail('the page never loaded');
if (started <= 0) fail('the page did not hand the fetched replay bytes to start()');

const platesL = document.getElementById('plates-l');
const platesR = document.getElementById('plates-r');
if (platesL.children.length !== 2 || platesR.children.length !== 2) {
  fail(`the scorebug built ${platesL.children.length}+${platesR.children.length} plates, want 2+2`);
}
const beats = document.getElementById('scrub').children.filter((c) => c.tag === 'button');
if (beats.length < 5) fail(`only ${beats.length} scrubber beats placed`);
for (const beat of beats) {
  if (!beat.attrs['aria-label']) fail('a scrubber beat has no aria-label');
  if (!(beat.listeners.click || []).length) fail('a scrubber beat does not seek on click');
}

// Drive playback: LOADING IS NOT PLAYING.
const clockAt = () => document.getElementById('clock-time').textContent;
const tickAt = () => document.getElementById('tick-clock').textContent;
const before = { clock: clockAt(), tick: tickAt() };
let now = 0;
for (let i = 0; i < 400 && frames.length; i++) {
  const fn = frames.shift();
  now += 32;
  fn(now);
}
const after = { clock: clockAt(), tick: tickAt() };
if (before.clock === after.clock || before.tick === after.tick) {
  fail(`playback did not advance: ${JSON.stringify(before)} -> ${JSON.stringify(after)}`);
}
if (!commands.some((c) => /^s:\d+$/.test(c))) fail('the renderer was never told which turn to draw');
const feed = document.getElementById('killfeed');
if (!feed.children.length) fail('the event feed drew nothing');

// Half speed and Space, driven through the shipped chrome. The 0.5x chip the
// chrome builds sends '5' down the command channel, ',' restarts playback
// from turn 0, and 100 frames at 32 ms (3200 ms) then advance ~12 turns
// (3200 * 0.5 / 125) where 1x would take ~25.
const lastTurn = () => {
  const seeks = commands.filter((c) => /^s:\d+$/.test(c));
  return seeks.length ? Number(seeks[seeks.length - 1].slice(2)) : -1;
};
const drive = (count) => {
  for (let i = 0; i < count && frames.length; i++) {
    const fn = frames.shift();
    now += 32;
    fn(now);
  }
};
const keydown = (key) => {
  let prevented = false;
  (documentListeners.keydown || []).forEach((fn) => {
    fn({ key, preventDefault() { prevented = true; } });
  });
  return prevented;
};
const chips = document.getElementById('speedchips').children;
const halfChip = chips.find((c) => c.text === '0.5×');
if (!halfChip) fail('the chrome built no 0.5x speed chip');
(halfChip.listeners.click || []).forEach((fn) => fn());
if (!halfChip.classList.contains('on')) fail('clicking the 0.5x chip did not select it');
keydown(',');
if (lastTurn() !== 0) fail('the , restart did not seek to turn 0');
drive(100);
const halfTurns = lastTurn();
if (halfTurns < 10 || halfTurns > 15) {
  fail(`0.5x advanced ${halfTurns} turns over 3200 ms, want ~12 (1x would be ~25)`);
}
if (!keydown(' ')) fail('Space did not preventDefault');
const pausedAt = lastTurn();
drive(30);
if (lastTurn() !== pausedAt) fail('Space did not pause playback');
if (!keydown(' ')) fail('the second Space did not preventDefault');
drive(30);
if (lastTurn() <= pausedAt) fail('the second Space did not resume playback');

console.log(JSON.stringify({
  loaded: true,
  startedBytes: started,
  plates: platesL.children.length + platesR.children.length,
  beats: beats.length,
  feed_lines: feed.children.length,
  clock: after.clock,
  tick: after.tick,
  commands: commands.length,
  half_speed_turns: halfTurns,
  space_pause: true,
}));
