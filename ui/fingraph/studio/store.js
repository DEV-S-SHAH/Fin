/* Application state.
 *
 * One object, one `set()`, one subscription list. Every module reads from here
 * and writes through `set`, so there is exactly one place where "what the app
 * believes" lives — the old page kept the same information spread across a
 * dozen globals, which is how the answer pane and the graph pane drifted out of
 * agreement about which entity was selected.
 *
 * `set` takes a patch and notifies only when a value actually changed, so a
 * listener that redraws the graph is not woken by a hover.
 */

import { loadPref } from "./util.js";

const listeners = new Set();

export const state = {
  /* data */
  stats: null,
  companies: [],
  entities: [],
  graph: { nodes: [], links: [], seeds: [] },
  graphLoading: false,
  selected: null,

  /* graph view */
  cited: new Set(),
  hiddenTypes: new Set(),
  showLabels: loadPref("labels", true),
  legendOpen: loadPref("legend", true),
  hops: loadPref("hops", 2),
  graphLimit: loadPref("graphLimit", 150),

  /* query */
  question: "",
  answer: null,
  streaming: "",
  busy: false,
  phase: null,
  tab: "answer",

  /* model backend */
  rag: null,

  /* ui */
  view: loadPref("view", "graph"),
  lastQuestion: loadPref("lastQuestion", ""),
};

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function set(patch) {
  let changed = false;
  const keys = [];
  for (const [key, value] of Object.entries(patch)) {
    if (state[key] === value) continue;
    state[key] = value;
    keys.push(key);
    changed = true;
  }
  if (changed) notify(keys);
  return changed;
}

/** Mutate a Set in place, then announce it under `key`. Sets are compared by
 *  reference, so `set({ cited })` with a new Set is how a change is detected. */
export function setCollection(key, next) {
  return set({ [key]: next instanceof Set ? next : new Set(next) });
}

function notify(keys) {
  for (const fn of listeners) {
    try {
      fn(state, keys);
    } catch (error) {
      // One broken listener must not stop the others, and must not take the
      // event handler down with it.
      console.error("state listener failed", error);
    }
  }
}
