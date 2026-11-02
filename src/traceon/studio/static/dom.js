// DOM construction helpers. Every string from a fixture reaches the page via
// text nodes or setAttribute, never through HTML parsing, so fixture content
// cannot inject markup or script.

const SVG_NS = "http://www.w3.org/2000/svg";

function apply(el, attrs) {
  if (!attrs) return;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.setAttribute("class", String(value));
    else if (key === "text") el.textContent = String(value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "style") Object.assign(el.style, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** Create an HTML element: h("div", {class: "x"}, "text", otherNode). */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  apply(el, attrs);
  append(el, children);
  return el;
}

/** Create an SVG element. */
export function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  apply(el, attrs);
  append(el, children);
  return el;
}

/** Replace all children of el. */
export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

/** Copy text to the clipboard; resolves to true on success. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    // Fall through to the selection-based fallback.
  }
  const area = h("textarea", { readonly: true, "aria-hidden": "true" });
  area.value = text;
  area.style.position = "fixed";
  area.style.top = "-1000px";
  document.body.append(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (err) {
    ok = false;
  }
  area.remove();
  return ok;
}

/** A small "Copy" button whose label confirms the result for a moment. */
export function copyButton(getText, label = "Copy") {
  const btn = h("button", { type: "button", class: "btn btn-quiet", title: "Copy to clipboard" }, label);
  btn.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const ok = await copyText(getText());
    btn.textContent = ok ? "Copied" : "Copy failed";
    window.setTimeout(() => { btn.textContent = label; }, 1200);
  });
  return btn;
}

export const storage = {
  get(key) {
    try { return window.localStorage.getItem(key); } catch (err) { return null; }
  },
  set(key, value) {
    try { window.localStorage.setItem(key, value); } catch (err) { /* storage unavailable */ }
  },
  remove(key) {
    try { window.localStorage.removeItem(key); } catch (err) { /* storage unavailable */ }
  },
};
