/* Rendering for an answer and everything the grader said about it.
 *
 * Two decisions here are inherited deliberately from the old page, because they
 * are about honesty rather than looks:
 *
 *   1. The violations block sits *above* the answer, not in a tab, not behind a
 *      disclosure. It is the one thing a reader must not miss, and it is capped
 *      at three plain-English lines by the server precisely so it can sit in the
 *      reading path.
 *   2. The verdict chip is the grader's verdict. Not `grounded`, which only
 *      asks whether the model cited something: a model that invents a figure and
 *      cites a real tag satisfies that and still fails, and colouring it green
 *      would be a lie the interface tells on purpose.
 */

import { clear, el, md, prettyType, typeOf } from "./util.js";

const VERDICTS = {
  SUPPORTED: {
    label: "Supported",
    desc: "Every sentence in this answer rests on a fact in the evidence it cites.",
  },
  QUALIFIED: {
    label: "Qualified",
    desc: "Nothing failed, but part of the answer is hedged or reaches past the filings.",
  },
  REFUSED: {
    label: "Refused",
    desc: "At least one sentence is not supported by the evidence it cites.",
  },
  /* The live-fetch path synthesises an answer from a filing that was not in the
   * graph when the session started, so there is no stored evidence to grade it
   * against. Saying "supported" there would be a claim nobody checked, and the
   * grader genuinely never ran. */
  SYNTHESISED: {
    label: "Live retrieval",
    desc: "This answer was written from a filing fetched just now and has not been graded. An ingest of that filing is queued, and the next answer about it will be checked against stored evidence.",
  },
};

const MIX_COLORS = {
  STATED: "var(--green)",
  DERIVED: "var(--accent)",
  INFERRED: "var(--amber)",
  EXTERNAL: "var(--violet)",
  GAP: "var(--red)",
};

export class AnswerView {
  /**
   * @param {{onCite: (id: string) => void, onTab: (name: string) => void}} handlers
   */
  constructor(handlers = {}) {
    this.handlers = handlers;
    this.panels = {
      answer: document.getElementById("tab-answer"),
      sources: document.getElementById("tab-sources"),
      trace: document.getElementById("tab-trace"),
      provenance: document.getElementById("tab-provenance"),
    };
    this.counts = {
      sources: document.getElementById("tab-n-sources"),
      provenance: document.getElementById("tab-n-provenance"),
    };
  }

  /* ── states ──────────────────────────────────────────────────────────── */

  empty(message = "Ask a question and the answer lands here, with every source it leaned on.") {
    clear(this.panels.answer).append(
      el("div", { class: "empty" }, [
        el("span", { class: "empty__icon", html: ICON_SEARCH }),
        el("strong", { text: "No question yet" }),
        el("span", { text: message }),
      ]),
    );
    clear(this.panels.sources).append(el("div", { class: "empty", text: "The entities the answer cites show up here." }));
    clear(this.panels.trace).append(el("div", { class: "empty", text: "The retrieval path shows up here after a question." }));
    clear(this.panels.provenance).append(
      el("div", { class: "empty", text: "Every sentence in the answer, with the rule that judged it." }),
    );
    this.counts.sources.textContent = "";
    this.counts.provenance.textContent = "";
  }

  /** The loading state. `streamed` is the partial text, if any has arrived. */
  pending(question, streamed = "") {
    const head = el("div", { class: "meta-row" }, [
      el("span", { class: "chip chip--accent", text: "working" }),
      el("span", { class: "chip", text: question.slice(0, 72) }),
    ]);
    if (!streamed) {
      head.append(el("div", { class: "spinner", html: `<span class="spinner__dot"></span> Retrieving graph context…` }));
    }
    const body = el("div", { class: "prose", html: streamed ? md(streamed) : "" });
    if (streamed) body.append(el("span", { class: "caret", text: "▍" }));
    clear(this.panels.answer).append(head, body);
  }

  error(message) {
    clear(this.panels.answer).append(
      el("p", { class: "notice notice--bad" }, [el("span", { html: ICON_ALERT }), el("span", { text: message })]),
    );
    this.emptyProvenance("No answer to grade — the last question failed.");
    clear(this.panels.trace);
  }

  emptyProvenance(message) {
    clear(this.panels.provenance).append(el("div", { class: "empty", text: message }));
    this.counts.provenance.textContent = "";
    this.counts.sources.textContent = "";
  }

  /** Append streamed text without rebuilding the panel — called per token. */
  appendStream(text) {
    const body = this.panels.answer.querySelector(".prose");
    if (!body) return;
    body.innerHTML = md(text);
    body.append(el("span", { class: "caret", text: "▍" }));
    this.panels.answer.parentElement?.scrollTo({ top: this.panels.answer.parentElement.scrollHeight });
  }

  /* ── the real thing ──────────────────────────────────────────────────── */

  render(res) {
    this.#renderAnswer(res);
    this.#renderSources(res);
    this.#renderTrace(res);
    this.#renderProvenance(res);
  }

  #renderAnswer(res) {
    const tagMap = res.tag_map || {};
    const nodesById = new Map(((res.graph || {}).nodes || []).map((n) => [n.id, n]));
    const meta = el("div", { class: "meta-row" });

    const chip = (text, className = "", title = "") =>
      el("span", { class: `chip ${className}`.trim(), text, title });

    meta.append(chip(`asked: ${(res.question || "").slice(0, 64)}`, "", "the question that produced this answer"));
    if (res.context_entities) {
      meta.append(chip(`${res.context_entities} entities · ${res.context_edges ?? 0} rels`, "",
        "the size of the subgraph handed to the model"));
    }
    if (res.route) meta.append(chip(res.route.toLowerCase(), "chip--accent", "which retrieval route answered this"));
    if (res.latency_ms || res.elapsed_sec) {
      const seconds = res.elapsed_sec ?? (res.latency_ms ? res.latency_ms / 1000 : null);
      meta.append(chip(seconds ? `${seconds.toFixed(1)}s` : "", "", "retrieval + generation time"));
    }

    const fragments = [meta];

    /* violations — first, unmissable, in the reading path */
    const violations = res.violations || [];
    if (violations.length) {
      fragments.push(el("div", { class: "violations" }, [
        el("div", { class: "violations__title" }, [
          el("span", { html: ICON_ALERT }),
          el("span", { text: "the grader refused part of this answer" }),
        ]),
        el("ul", {}, violations.map((v) => el("li", { text: v }))),
      ]));
    }

    fragments.push(this.#verdictCard(res));

    const body = el("div", { class: "prose", html: md(res.text || res.answer || "") });
    body.addEventListener("click", (event) => {
      const button = event.target.closest(".cite");
      if (!button) return;
      const id = tagMap[button.dataset.tag];
      // A tag the retriever never issued cannot be focused, and a citation chip
      // that goes nowhere teaches the reader that citations are decorative.
      if (id) this.handlers.onCite?.(id);
      else toastInline(button, "that tag was never issued by the retriever");
    });
    fragments.push(body);

    if (res.reasoning) {
      const details = el("details", { class: "reasoning" }, [
        el("summary", { text: `Model reasoning (${res.rag_model || "model"})` }),
        el("div", { class: "reasoning__body", text: res.reasoning }),
      ]);
      fragments.push(details);
    }

    if (res.gap) {
      fragments.push(el("p", { class: "notice" }, [
        el("span", { html: ICON_INFO }),
        el("span", { text: "The corpus does not support an answer to this question, so the text above is a rendered gap rather than an answer. The Provenance tab shows the rules applied to what the model wrote before the replacement." }),
      ]));
    }

    clear(this.panels.answer).append(...fragments);
  }

  #verdictCard(res) {
    const ungraded = !res.verdict && res.route === "COLD_START";
    const verdict = res.verdict || (ungraded ? "SYNTHESISED" : "REFUSED");
    const spec = VERDICTS[verdict] || { label: verdict, desc: "the grader returned a verdict this page does not know" };
    const mix = res.provenance_mix || {};
    const mixKeys = Object.keys(mix).sort();

    const card = el("div", { class: `verdict verdict--${verdict}` });
    card.append(
      el("div", { class: "verdict__top" }, [
        el("span", {
          class: "verdict__icon",
          html: verdict === "SUPPORTED" ? ICON_CHECK
            : verdict === "QUALIFIED" || verdict === "SYNTHESISED" ? ICON_QUALIFIED
            : ICON_ALERT,
        }),
        el("span", { class: "verdict__label", text: spec.label }),
        el("div", { class: "topbar__spacer" }),
        ...["invented_tags", "ungrounded_figures", "misattributed"].map((key) => {
          const values = res[key] || [];
          if (!values.length) return null;
          const title = {
            invented_tags: "cited tags the retriever never issued",
            ungrounded_figures: "figures that appear in no cited source",
            misattributed: "issuers no cited source was filed by",
          }[key];
          return el("span", { class: "chip chip--bad", text: `${values.length} ${key === "invented_tags" ? "invented tag" : key === "ungrounded_figures" ? "loose figure" : "misattributed"}`, title: `${title}: ${values.join(", ")}` });
        }).filter(Boolean),
      ]),
      el("p", { class: "verdict__desc", text: spec.desc }),
    );

    if (mixKeys.length) {
      const total = mixKeys.reduce((sum, key) => sum + (mix[key] || 0), 0) || 1;
      const bar = el("div", { class: "mixbar", role: "img", "aria-label": mixKeys.map((k) => `${k}: ${mix[k]}`).join(", ") });
      for (const key of mixKeys) {
        bar.append(el("span", { style: `flex:${mix[key] / total};background:${MIX_COLORS[key] || "var(--text-faint)"}` }));
      }
      const legend = el("div", { class: "mix-legend" }, mixKeys.map((key) => el("span", { class: "mix-legend__item" }, [
        el("span", { class: "mix-legend__dot", style: `background:${MIX_COLORS[key] || "var(--text-faint)"}` }),
        el("span", { text: `${key} ${mix[key]}` }),
      ])));
      card.append(bar, legend);
    }
    return card;
  }

  #renderSources(res) {
    const host = clear(this.panels.sources);
    const tagMap = res.tag_map || {};
    const nodesById = new Map(((res.graph || {}).nodes || []).map((n) => [n.id, n]));
    const tags = res.used_tags || [];

    this.counts.sources.textContent = tags.length ? String(tags.length) : "";
    if (!tags.length) {
      host.append(el("div", { class: "empty", text: "The model cited no entities in this answer." }));
      return;
    }

    const list = el("div", { class: "stack" });
    for (const tag of tags) {
      const id = tagMap[tag];
      const node = nodesById.get(id);
      const description = node?.description || "";
      const value = /value=[^ ]+/.exec(description)?.[0]?.slice(6) || "";

      const row = el("button", {
        class: "source",
        type: "button",
        title: description || node?.name || "",
        onclick: () => { if (id) this.handlers.onCite?.(id); },
      }, [
        el("span", { class: "source__tag", text: tag }),
        el("span", { class: "source__name", text: node?.name || "(not in this view)" }),
        value ? el("span", { class: "source__val", text: value }) : null,
        el("span", { class: "source__type", text: node ? prettyType(typeOf(node)) : "" }),
      ]);
      list.append(row);
    }
    host.append(list);
  }

  #renderTrace(res) {
    const host = clear(this.panels.trace);
    if (res.flow) {
      host.append(el("pre", { class: "trace", text: res.flow }));
      return;
    }
    const lines = [
      res.route ? `route: ${res.route}` : null,
      res.context_entities ? `retrieved: ${res.context_entities} entities, ${res.context_edges ?? 0} relationships` : null,
      res.grounded !== undefined ? `grounded: ${res.grounded}` : null,
      (res.used_tags || []).length ? `cited: ${res.used_tags.join(", ")}` : "cited: none",
      res.background_task_scheduled ? "background ingestion: scheduled" : null,
    ].filter(Boolean);
    if (lines.length) {
      host.append(el("pre", { class: "trace", text: lines.join("\n") }));
      return;
    }
    host.append(el("div", { class: "empty", text: "No retrieval trace for this answer." }));
  }

  #renderProvenance(res) {
    const host = clear(this.panels.provenance);
    /* Two shapes again. A graded answer carries a list of per-claim verdicts; the
     * live-fetch path returns one pre-rendered ledger as a string. Iterating a
     * string here would produce a card per character. */
    const raw = res.provenance;
    if (typeof raw === "string" && raw.trim()) {
      this.counts.provenance.textContent = "";
      host.append(el("pre", { class: "prov-ledger", text: raw }));
      return;
    }
    const verdicts = Array.isArray(raw) ? raw : [];
    this.counts.provenance.textContent = verdicts.length ? String(verdicts.length) : "";

    if (!verdicts.length) {
      host.append(el("div", {
        class: "empty",
        text: res.verdict
          ? "The grader judged no sentences in this answer."
          : "Every sentence in the answer, with the rule that judged it.",
      }));
      return;
    }

    const tagMap = res.tag_map || {};

    /* When every sentence failed, the server replaced the answer text with a
     * rendered gap. These verdicts still describe what the model wrote, so
     * without this the tab shows sentences the answer above does not contain and
     * a reader concludes nothing was checked. */
    if (res.gap) {
      host.append(el("p", { class: "notice" }, [
        el("span", { html: ICON_INFO }),
        el("span", { text: "The answer above was replaced because every sentence failed. These are the rules applied to what the model wrote before the replacement." }),
      ]));
    }

    const mix = res.provenance_mix || {};
    const mixKeys = Object.keys(mix).sort();
    if (mixKeys.length) {
      const head = el("div", { class: "meta-row" }, [
        ...mixKeys.map((key) => el("span", { class: "chip", text: `${key} ${mix[key]}` })),
      ]);
      const invented = res.invented_tags || [];
      if (invented.length) {
        head.append(el("span", { class: "chip chip--bad", text: `invented tags: ${invented.join(", ")}`, title: "cited tags the retriever never issued" }));
      }
      const loose = res.ungrounded_figures || [];
      if (loose.length) {
        head.append(el("span", { class: "chip chip--bad", text: `figures not in any cited source: ${loose.join(", ")}` }));
      }
      const wrong = res.misattributed || [];
      if (wrong.length) {
        head.append(el("span", { class: "chip chip--bad", text: `attributed to: ${wrong.join(", ")}`, title: "issuers no cited source was filed by" }));
      }
      host.append(head);
    }

    for (const verdict of verdicts) {
      const tag = verdict.provenance || "";
      const row = el("div", { class: `prov-row prov-row--${tag}` });
      row.append(el("span", { class: "prov-badge", text: tag || "—", title: "the rule that judged this sentence" }));

      const body = el("div", { class: "prov-row__body" });
      const text = el("div", { class: "prov-row__text", text: verdict.text || "" });
      /* One 400-word sentence would otherwise turn this tab into a wall and
       * hide every row under it. */
      if ((verdict.text || "").length > 240) {
        text.classList.add("is-clamped");
        text.title = "click to expand";
        text.addEventListener("click", () => text.classList.toggle("is-clamped"));
      }
      body.append(text);

      const flags = [];
      for (const cite of verdict.unknown_cites || []) flags.push(["", `cited a tag never issued: ${cite}`]);
      for (const figure of verdict.ungrounded || []) flags.push(["", `figure not in any cited source: ${figure}`]);
      for (const filer of verdict.misattributed || []) flags.push(["", `attributed to ${filer}, which no cited source was filed by`]);
      for (const cite of verdict.cites || []) flags.push(["flag--cite", `[${cite}]`]);

      if (flags.length) {
        body.append(el("div", { class: "prov-row__flags" }, flags.map(([className, label]) => {
          if (className === "flag--cite") {
            return el("button", {
              class: "flag flag--cite",
              type: "button",
              text: label,
              onclick: () => { const id = tagMap[label.replace(/[\[\]]/g, "")]; if (id) this.handlers.onCite?.(id); },
            });
          }
          return el("span", { class: "flag", text: label });
        })));
      }

      if (verdict.reason) body.append(el("div", { class: "prov-row__reason", text: verdict.reason }));
      row.append(body);
      host.append(row);
    }
  }
}

/* Small in-place note next to a citation that cannot be followed. */
function toastInline(anchor, message) {
  const tip = el("span", {
    text: ` (${message})`,
    style: "font-size:var(--text-xs);color:var(--amber)",
  });
  anchor.after(tip);
  setTimeout(() => tip.remove(), 4000);
}

/* Icons, inlined so there is no sprite request and no icon-font dependency. */const ICON_CHECK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 13 4.5 4.5L19 7"/></svg>`;
const ICON_QUALIFIED = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8.5 12h7"/></svg>`;
const ICON_ALERT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 8v5"/><circle cx="12" cy="16.5" r="1.1" fill="currentColor" stroke="none"/></svg>`;
const ICON_INFO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r="1.05" fill="currentColor" stroke="none"/></svg>`;
const ICON_SEARCH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>`;