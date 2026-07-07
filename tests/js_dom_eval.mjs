// Minimal pure-JS DOM mock + expression evaluator for client.py tests.
//
// client.py builds CSS/DOM expressions as JS strings and sends them to the page
// via Runtime.evaluate. To exercise that *real* JS (the innermost-match walk in
// click_text, the selector read/fill primitives) without a browser, the tests'
// fake bridge pipes each Runtime.evaluate expression here: this script builds a
// tiny DOM from a JSON fixture, evaluates the expression against it, and prints
// the JSON-serialised return value back.
//
// Only the surface the primitives touch is implemented:
//   document.querySelector / querySelectorAll, element.querySelectorAll,
//   textContent, innerText, value, getAttribute, focus, dispatchEvent,
//   getBoundingClientRect, window.getComputedStyle, new Event(...).
//
// Input  (stdin JSON): { "fixture": <node>, "expr": <string> }
//   node := { "tag": "div",
//             "attrs": { "class": "x", "data-foo": "..." }?,
//             "text": "leaf text"?,           // only on leaf nodes
//             "value": "textarea value"?,
//             "rect": [w, h]?,                // bounding box (default 10x10)
//             "style": { "display": "...", "visibility": "..." }?,
//             "children": [ <node>, ... ]? }
// Output (stdout JSON): { "value": <evaluated result> }  or { "error": <msg> }

import { readFileSync } from "node:fs";

let dispatched = []; // record dispatched DOM events for assertions if needed

class FakeElement {
  constructor(spec, parent) {
    this.tagName = (spec.tag || "div").toUpperCase();
    this.parentNode = parent || null;
    this._attrs = Object.assign({}, spec.attrs || {});
    if (spec.class && this._attrs.class === undefined) this._attrs.class = spec.class;
    this._ownText = spec.text != null ? String(spec.text) : "";
    this.value = spec.value != null ? spec.value : undefined;
    this._rect = spec.rect || [10, 10];
    this._style = Object.assign(
      { display: "block", visibility: "visible" },
      spec.style || {}
    );
    this.children = (spec.children || []).map((c) => new FakeElement(c, this));
  }

  get className() {
    return this._attrs.class || "";
  }

  // textContent = own text + all descendants' text (DOM semantics).
  get textContent() {
    let t = this._ownText || "";
    for (const c of this.children) t += c.textContent;
    return t;
  }

  // innerText approximated as textContent (good enough for the label reads).
  get innerText() {
    return this.textContent;
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name)
      ? this._attrs[name]
      : null;
  }

  getBoundingClientRect() {
    const [w, h] = this._rect;
    return { left: 0, top: 0, width: w, height: h };
  }

  focus() {
    this._focused = true;
  }

  dispatchEvent(ev) {
    dispatched.push({ type: ev.type, tag: this.tagName });
    return true;
  }

  _self_and_descendants() {
    const out = [this];
    for (const c of this.children) out.push(...c._self_and_descendants());
    return out;
  }

  _matchAttr(attrSel) {
    // attrSel like "name=contact-message" or just "name" (brackets stripped).
    const m = /^([^=]+)(?:=['"]?([^'"]*)['"]?)?$/.exec(attrSel);
    if (!m) return false;
    const [, name, val] = m;
    if (val === undefined) return this.getAttribute(name) !== null;
    return this.getAttribute(name) === val;
  }

  _matches(sel) {
    sel = sel.trim();
    if (sel === "*") return true;
    // Optional leading tag, then any run of .class / #id / [attr] qualifiers.
    const head = /^([a-zA-Z][\w-]*)?/.exec(sel)[1] || null;
    if (head && this.tagName !== head.toUpperCase()) return false;
    let rest = head ? sel.slice(head.length) : sel;
    const tokens = rest.match(/\.[\w-]+|#[\w-]+|\[[^\]]+\]/g) || [];
    if (!head && tokens.length === 0) return false;
    for (const tok of tokens) {
      if (tok[0] === ".") {
        if (!(this.className || "").split(/\s+/).includes(tok.slice(1))) return false;
      } else if (tok[0] === "#") {
        if (this._attrs.id !== tok.slice(1)) return false;
      } else if (tok[0] === "[") {
        if (!this._matchAttr(tok.slice(1, -1))) return false;
      }
    }
    return true;
  }

  querySelectorAll(sel) {
    // descendants only (exclude self), honour comma lists, preserve doc order.
    const sels = sel.split(",").map((s) => s.trim());
    const all = this._self_and_descendants().slice(1); // drop self
    return all.filter((el) => sels.some((s) => el._matches(s)));
  }

  querySelector(sel) {
    const list = this.querySelectorAll(sel);
    return list.length ? list[0] : null;
  }
}

function run() {
  const input = JSON.parse(readFileSync(0, "utf8"));
  const rootChild = new FakeElement(input.fixture, null);
  // documentElement wraps the fixture so document.querySelector* search it too.
  const docEl = new FakeElement(
    { tag: "html", children: [input.fixture] },
    null
  );
  const document = {
    documentElement: docEl,
    querySelector: (s) => docEl.querySelector(s),
    querySelectorAll: (s) => docEl.querySelectorAll(s),
  };
  const window = {
    getComputedStyle: (el) => el._style,
  };
  class Event {
    constructor(type) {
      this.type = type;
    }
  }
  // eslint-disable-next-line no-unused-vars
  const result = (function (document, window, Event) {
    return eval(input.expr);
  })(document, window, Event);
  process.stdout.write(JSON.stringify({ value: result === undefined ? null : result }));
}

try {
  run();
} catch (e) {
  process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
}
