// Pure, DOM-free logic shared by the views: playback timeline math and the
// choice of a sensible diff partner. Unit-tested from tests/studio with node.

const CALL_KINDS = new Set(["tool", "model", "value"]);

/**
 * Ordered call timeline for playback.
 * `starts[i]` / `durations[i]` are offsets in ms, parallel to `kinds[i]`.
 * Returns { order, times, total } where `order` lists call indexes by start
 * time (ties keep sequence order) and `times` holds their start offsets.
 */
export function buildTimeline(kinds, starts, durations, total) {
  const order = [];
  for (let i = 0; i < kinds.length; i++) if (CALL_KINDS.has(kinds[i])) order.push(i);
  order.sort((a, b) => (starts[a] - starts[b]) || (a - b));
  const times = order.map((i) => starts[i]);
  let end = Number.isFinite(total) && total > 0 ? total : 0;
  for (let i = 0; i < kinds.length; i++) end = Math.max(end, starts[i] + (durations[i] || 0));
  return { order, times, total: end > 0 ? end : 1 };
}

/** Index into `times` of the last call that started at or before t; -1 if none. */
export function callAt(times, t) {
  let lo = 0, hi = times.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t + 1e-9) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;
}

/** Start time of the next call strictly after t, or null at the end. */
export function nextCallTime(times, t) {
  const k = callAt(times, t);
  for (let j = k + 1; j < times.length; j++) if (times[j] > t + 1e-6) return times[j];
  return null;
}

/** Start time of the previous call strictly before t, or 0. */
export function prevCallTime(times, t) {
  for (let j = callAt(times, t); j >= 0; j--) if (times[j] < t - 1e-6) return times[j];
  return 0;
}

/** Fraction of an event [start, start+dur] that has played at time t (0..1). */
export function progress(start, dur, t) {
  if (t < start) return 0;
  if (!(dur > 0)) return 1;
  return Math.min(1, (t - start) / dur);
}

/**
 * Uniform playback: each call gets the same wall time. Map a fractional call
 * cursor u (0 .. times.length) to a time on the run's axis.
 */
export function uniformTime(times, total, u) {
  const n = times.length;
  if (n === 0) return Math.min(total, Math.max(0, u));
  if (u <= 0) return 0;
  if (u >= n) return total;
  const k = Math.floor(u);
  const a = times[k];
  const b = k + 1 < n ? times[k + 1] : total;
  return a + (b - a) * (u - k);
}

/** Inverse of uniformTime: the cursor for time t. */
export function uniformCursor(times, total, t) {
  const n = times.length;
  if (n === 0 || t <= 0) return 0;
  if (t >= total) return n;
  const k = callAt(times, t);
  if (k < 0) return 0;
  const a = times[k];
  const b = k + 1 < n ? times[k + 1] : total;
  return k + (b > a ? (t - a) / (b - a) : 0);
}

/** Run name without a trailing parenthetical: "agent (prompt v2)" -> "agent". */
export function baseName(name) {
  return String(name || "").replace(/\s*\([^()]*\)\s*$/, "").trim().toLowerCase();
}

function firstWord(name) {
  return baseName(name).split(/[\s\-_:./]+/).filter(Boolean)[0] || "";
}

/**
 * Pick the fixture to compare `id` against. Preference: same run name
 * (ignoring a trailing parenthetical), then same leading name word, then the
 * most recent other fixture. `summaries` must be newest first.
 */
export function pickPartner(summaries, id) {
  const self = summaries.find((s) => s.id === id);
  const others = summaries.filter((s) => s.id !== id);
  if (!others.length) return null;
  if (!self) return others[0].id;
  const name = baseName(self.name);
  const exact = others.find((s) => baseName(s.name) === name);
  if (exact) return exact.id;
  const prefix = others.find((s) => {
    const other = baseName(s.name);
    return name && other && (other.startsWith(name) || name.startsWith(other));
  });
  if (prefix) return prefix.id;
  const word = firstWord(self.name);
  const sameWord = word ? others.find((s) => firstWord(s.name) === word) : null;
  if (sameWord) return sameWord.id;
  return others[0].id;
}

/**
 * Default [baseline, candidate] for diff mode around `currentId` (may be null).
 * The older of the pair is the baseline.
 */
export function defaultPair(summaries, currentId) {
  if (summaries.length < 2) return [null, null];
  let current = currentId && summaries.some((s) => s.id === currentId) ? currentId : null;
  if (!current) {
    // No run in focus: the newest fixture that has a same-name sibling, else the newest.
    const withSibling = summaries.find((s) => summaries.some((o) => o.id !== s.id && baseName(o.name) === baseName(s.name)));
    current = (withSibling || summaries[0]).id;
  }
  const other = pickPartner(summaries, current);
  const ids = summaries.map((s) => s.id);
  return ids.indexOf(current) < ids.indexOf(other) ? [other, current] : [current, other];
}
