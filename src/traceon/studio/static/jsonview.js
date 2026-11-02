// Collapsible JSON tree. Children are built lazily on first expand so large
// payloads (up to the 256 KB capture limit) stay cheap to open.

import { h } from "./dom.js";
import { keyPath } from "./format.js";

const PAGE = 200;
const MAX_STRING = 4000;
const REDACTED = /^\[REDACTED(?::[^\]]*)?\]$/;

function typeOf(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  return typeof v;
}

function primitive(value) {
  const t = typeOf(value);
  if (t === "string") {
    if (REDACTED.test(value)) {
      return h("span", { class: "jv-redacted", title: "Replaced by the redaction policy" }, value);
    }
    let text = JSON.stringify(value);
    let extra = "";
    if (text.length > MAX_STRING) {
      extra = ` … ${text.length - MAX_STRING} more chars`;
      text = text.slice(0, MAX_STRING);
    }
    return h("span", { class: "jv-str" }, text, extra ? h("span", { class: "jv-summary" }, extra) : null);
  }
  if (t === "number") return h("span", { class: "jv-num" }, String(value));
  if (t === "boolean") return h("span", { class: "jv-bool" }, String(value));
  if (t === "null") return h("span", { class: "jv-null" }, "null");
  return h("span", { class: "jv-summary" }, String(value));
}

function summary(value) {
  if (Array.isArray(value)) return value.length === 1 ? "1 item" : `${value.length} items`;
  const n = Object.keys(value).length;
  return n === 1 ? "1 key" : `${n} keys`;
}

function keyLabel(key, isIndex) {
  if (key === null) return null;
  if (isIndex) return [h("span", { class: "jv-punct" }, `${key}: `)];
  return [h("span", { class: "jv-key" }, JSON.stringify(key)), h("span", { class: "jv-punct" }, ": ")];
}

function node(key, isIndex, value, depth, opts, path) {
  const t = typeOf(value);
  const trailing = opts.last ? null : h("span", { class: "jv-punct" }, ",");
  if (t !== "object" && t !== "array") {
    return h("div", { class: "jv-line", role: "treeitem", title: path },
      h("span", { class: "jv-pad" }), keyLabel(key, isIndex), primitive(value), trailing);
  }
  const entries = t === "array" ? value.map((v, i) => [i, v]) : Object.entries(value);
  const [open, close] = t === "array" ? ["[", "]"] : ["{", "}"];
  if (entries.length === 0) {
    return h("div", { class: "jv-line", role: "treeitem", title: path },
      h("span", { class: "jv-pad" }), keyLabel(key, isIndex), h("span", { class: "jv-punct" }, open + close), trailing);
  }
  const wrap = h("div", { role: "treeitem", "aria-expanded": "false" });
  const toggle = h("button", { type: "button", class: "jv-toggle", "aria-label": "Expand", tabindex: "-1" }, "▸");
  const collapsedTail = h("span", { class: "jv-summary" }, ` ${summary(value)} `);
  const collapsedClose = h("span", { class: "jv-punct" }, close);
  const head = h("div", { class: "jv-line", title: path }, toggle, keyLabel(key, isIndex),
    h("span", { class: "jv-punct" }, open), collapsedTail, collapsedClose, opts.last ? null : trailing);
  const children = h("div", { class: "jv-children", role: "group" });
  const closeLine = h("div", { class: "jv-line" }, h("span", { class: "jv-pad" }),
    h("span", { class: "jv-punct" }, close), opts.last ? null : h("span", { class: "jv-punct" }, ","));
  let built = 0;

  function buildMore() {
    const end = Math.min(entries.length, built + PAGE);
    const frag = document.createDocumentFragment();
    for (let i = built; i < end; i++) {
      const [k, v] = entries[i];
      const childPath = t === "array" ? `${path}[${k}]` : keyPath(path, String(k));
      frag.append(node(k, t === "array", v, depth + 1, { ...opts, last: i === entries.length - 1 }, childPath));
    }
    built = end;
    const old = children.querySelector(":scope > .jv-more");
    if (old) old.remove();
    children.append(frag);
    if (built < entries.length) {
      const rest = entries.length - built;
      children.append(h("button", {
        type: "button", class: "btn btn-quiet jv-more",
        onclick: (ev) => { ev.stopPropagation(); buildMore(); },
      }, `Show ${Math.min(PAGE, rest)} more of ${rest}`));
    }
  }

  function set(expanded) {
    wrap.setAttribute("aria-expanded", String(expanded));
    toggle.textContent = expanded ? "▾" : "▸";
    toggle.setAttribute("aria-label", expanded ? "Collapse" : "Expand");
    collapsedTail.hidden = expanded;
    collapsedClose.hidden = expanded;
    if (expanded && built === 0) buildMore();
    children.hidden = !expanded;
    closeLine.hidden = !expanded;
  }

  toggle.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const open = wrap.getAttribute("aria-expanded") !== "true";
    set(open);
    children.classList.remove("is-opening");
    if (open) {
      void children.offsetWidth;
      children.classList.add("is-opening");
    }
  });
  wrap.append(head, children, closeLine);
  set(depth < opts.expandDepth);
  return wrap;
}

/** Render a JSON value as a collapsible tree. */
export function jsonTree(value, { expandDepth = 2, label = "JSON value" } = {}) {
  const root = h("div", { class: "jv", role: "tree", "aria-label": label });
  root.append(node(null, false, value, 0, { expandDepth, last: true }, "$"));
  return root;
}
