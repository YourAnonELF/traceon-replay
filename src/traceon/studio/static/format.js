// Number, time and identifier formatting shared by every view.

const intFmt = new Intl.NumberFormat("en-US");

/** Parse an ISO-8601 timestamp to epoch milliseconds, keeping sub-ms digits. */
export function parseTs(iso) {
  if (typeof iso !== "string") return NaN;
  const m = /^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$/.exec(iso);
  if (!m) return Date.parse(iso);
  const base = Date.parse(m[1] + (m[3] || "Z"));
  const frac = m[2] ? Number("0." + m[2]) * 1000 : 0;
  return base + frac;
}

/** Compact duration: 0.42 ms, 7.1 ms, 612 ms, 1.47 s, 2m 05s. */
export function fmtMs(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "–";
  const a = Math.abs(ms);
  if (a < 1) return `${ms.toFixed(2)} ms`;
  if (a < 10) return `${ms.toFixed(1)} ms`;
  if (a < 1000) return `${Math.round(ms)} ms`;
  if (a < 60000) return `${(ms / 1000).toFixed(a < 10000 ? 2 : 1)} s`;
  const total = Math.round(a / 1000);
  const sign = ms < 0 ? "-" : "";
  return `${sign}${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s`;
}

/** Exact duration with microsecond precision, for tooltips and detail. */
export function fmtExactMs(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "–";
  return `${ms.toFixed(3)} ms`;
}

/** Axis tick label: 0, 250 ms, 1 s, 1.5 s. */
export function fmtTick(ms, step) {
  if (ms === 0) return "0";
  if (step >= 1000) return `${+(ms / 1000).toFixed(step % 1000 === 0 ? 0 : 1)} s`;
  if (step < 1) return `${ms.toFixed(2)} ms`;
  return `${+ms.toFixed(step < 10 ? 1 : 0)} ms`;
}

export function fmtInt(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "–";
  return intFmt.format(n);
}

export function fmtCost(amount, currency) {
  if (amount === null || amount === undefined || !Number.isFinite(amount)) return "–";
  const digits = Math.abs(amount) >= 1 ? 2 : 4;
  if (!currency || currency === "USD") return `$${amount.toFixed(digits)}`;
  return `${amount.toFixed(digits)} ${currency}`;
}

export function fmtBytes(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** "just now", "12m ago", "3h ago", "4d ago", or a date for older runs. */
export function relTime(iso, now = Date.now()) {
  const t = parseTs(iso);
  if (!Number.isFinite(t)) return "";
  const sec = Math.round((now - t) / 1000);
  if (sec < 0) return new Date(t).toISOString().slice(0, 10);
  if (sec < 45) return "just now";
  if (sec < 3600) return `${Math.max(1, Math.round(sec / 60))}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  if (sec < 30 * 86400) return `${Math.round(sec / 86400)}d ago`;
  return new Date(t).toISOString().slice(0, 10);
}

/** Short form of a run/fixture id: "run_2b9e4f10a7c3" -> "2b9e4f10". */
export function shortId(id) {
  if (typeof id !== "string") return "";
  const tail = id.includes("_") ? id.slice(id.indexOf("_") + 1) : id;
  return tail.length > 8 ? tail.slice(0, 8) : tail;
}

export function shortHash(sha) {
  if (typeof sha !== "string") return "";
  const hex = sha.startsWith("sha256:") ? sha.slice(7) : sha;
  return `sha256:${hex.slice(0, 10)}`;
}

/** Signed delta for the metrics table. */
export function fmtDelta(a, b, kind) {
  if (a === null || a === undefined || b === null || b === undefined) return { text: "–", cls: "delta-zero" };
  const d = b - a;
  if (Math.abs(d) < 1e-9) return { text: "0", cls: "delta-zero" };
  const sign = d > 0 ? "+" : "−";
  let body;
  if (kind === "ms") body = fmtMs(Math.abs(d));
  else if (kind === "cost") body = fmtCost(Math.abs(d));
  else body = fmtInt(Math.abs(d));
  let pct = "";
  if (a !== 0 && kind !== "count") pct = ` (${sign}${Math.abs((d / a) * 100).toFixed(1)}%)`;
  return { text: `${sign}${body}${pct}`, cls: d > 0 ? "delta-up" : "delta-down" };
}

/** Compact one-line JSON for expected/actual values. */
export function oneLine(value, max = 400) {
  let text;
  if (value === undefined) text = "(absent)";
  else {
    try { text = JSON.stringify(value); } catch (err) { text = String(value); }
  }
  if (text === undefined) text = String(value);
  return text.length > max ? `${text.slice(0, max)}… (${text.length - max} more chars)` : text;
}

/** Identifier-like keys use .key, others ["key"] -- mirrors traceon.core.compare. */
export function keyPath(path, key) {
  if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return `${path}.${key}`;
  return `${path}["${key.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"]`;
}
