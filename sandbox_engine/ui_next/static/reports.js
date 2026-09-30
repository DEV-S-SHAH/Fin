/* The canned-reports drawer.
 *
 * Five fixed Cypher reports, no LLM, so the honest presentation is a data table
 * and not a chat bubble: sortable columns, a row filter that works on every
 * column at once, and a CSV of exactly what is on screen.
 */

import { fetchReport, fetchReports } from "./api.js";
import { $, clear, el, fmtNumber, fmtScale, plural, toast } from "./util.js";

const NUMERIC_HINT = /^(value|scale|fiscal_year|year|count|amount|percent)$/i;

export class ReportsPanel {
  constructor() {
    this.drawer = $("drawer");
    this.scrim = $("scrim");
    this.tabs = $("report-tabs");
    this.body = $("report-body");
    this.filter = $("report-filter");
    this.meta = [];
    this.active = null;
    this.result = null;
    this.sort = { column: -1, direction: 1 };
    this.lastFocus = null;

    this.#wire();
  }

  #wire() {
    $("reports-btn").addEventListener("click", () => this.open());
    $("drawer-close").addEventListener("click", () => this.close());
    this.scrim.addEventListener("click", () => this.close());
    this.filter.addEventListener("input", () => this.#renderTable());
    $("csv-btn").addEventListener("click", () => this.#downloadCsv());

    // Focus has to come back to whatever opened the drawer, or the keyboard
    // user is dropped at the top of the document.
    this.drawer.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const focusable = this.drawer.querySelectorAll(
        'button:not([disabled]), input, [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  async open() {
    this.lastFocus = document.activeElement;
    this.drawer.hidden = false;
    this.scrim.hidden = false;
    // One frame of `hidden` removal first: transitioning an element that is
    // still display:none animates nothing and the drawer just appears.
    requestAnimationFrame(() => {
      this.drawer.classList.add("is-open");
      this.scrim.classList.add("is-open");
    });
    $("drawer-close").focus();

    if (!this.meta.length) await this.#loadList();
    if (!this.result && this.meta.length) await this.run(this.meta[0].id);
  }

  close() {
    this.drawer.classList.remove("is-open");
    this.scrim.classList.remove("is-open");
    setTimeout(() => {
      this.drawer.hidden = true;
      this.scrim.hidden = true;
    }, 220);
    this.lastFocus?.focus?.();
  }

  get isOpen() {
    return this.drawer.classList.contains("is-open");
  }

  async #loadList() {
    clear(this.tabs).append(el("div", { class: "spinner", html: `<span class="spinner__dot"></span> Loading reports…` }));
    try {
      const data = await fetchReports();
      this.meta = data.reports || [];
    } catch (error) {
      clear(this.tabs).append(el("p", { class: "notice notice--bad", text: `Could not list reports: ${error.message}` }));
      return;
    }
    clear(this.tabs);
    for (const report of this.meta) {
      const button = el("button", {
        class: "tab",
        type: "button",
        role: "tab",
        text: report.title,
        title: report.description || "",
        dataset: { id: report.id },
        onclick: () => this.run(report.id),
      });
      this.tabs.append(button);
    }
  }

  async run(id) {
    this.active = id;
    for (const button of this.tabs.querySelectorAll("button")) {
      const on = button.dataset.id === id;
      button.setAttribute("aria-selected", on ? "true" : "false");
      button.classList.toggle("is-on", on);
    }
    clear(this.body).append(el("div", { class: "spinner", html: `<span class="spinner__dot"></span> Running the report against the graph…` }));
    this.filter.value = "";
    this.sort = { column: -1, direction: 1 };
    try {
      const data = await fetchReport(id);
      if (data.error) {
        clear(this.body).append(el("p", { class: "notice notice--bad", text: data.error }));
        return;
      }
      this.result = data;
      this.#render();
    } catch (error) {
      clear(this.body).append(el("p", { class: "notice notice--bad", text: `Report failed: ${error.message}` }));
      toast(`report failed: ${error.message}`, "bad");
    }
  }

  #render() {
    const data = this.result;
    const host = clear(this.body);
    host.append(
      el("div", { class: "report-head" }, [
        el("h3", { text: data.title || "Report" }),
        el("p", { text: data.description || "" }),
      ]),
    );
    this.#renderTable();
  }

  #filteredRows() {
    const data = this.result;
    if (!data) return [];
    const term = (this.filter.value || "").trim().toLowerCase();
    let rows = data.rows || [];
    if (term) {
      rows = rows.filter((row) => row.some((cell) => String(cell ?? "").toLowerCase().includes(term)));
    }
    const { column, direction } = this.sort;
    if (column >= 0) {
      rows = [...rows].sort((a, b) => {
        const left = a[column];
        const right = b[column];
        if (left === right) return 0;
        if (left === null || left === undefined) return 1;
        if (right === null || right === undefined) return -1;
        if (typeof left === "number" && typeof right === "number") return (left - right) * direction;
        return String(left).localeCompare(String(right), undefined, { numeric: true }) * direction;
      });
    }
    return rows;
  }

  #renderTable() {
    const data = this.result;
    if (!data) return;
    const host = clear(this.body);
    const columns = data.columns || [];
    const rows = this.#filteredRows();
    const total = data.rows?.length ?? 0;

    if (!total) {
      host.append(el("div", { class: "empty" }, [
        el("span", { class: "empty__icon", html: ICON_TABLE }),
        el("strong", { text: "No rows" }),
        el("span", { text: "This report returned nothing — the graph holds no matching data yet." }),
      ]));
      return;
    }
    if (!rows.length) {
      host.append(el("div", { class: "empty", text: `No row matches “${this.filter.value}”.` }));
      return;
    }

    const table = el("table", { class: "data" });
    const thead = el("thead");
    const headRow = el("tr");
    columns.forEach((column, index) => {
      const active = this.sort.column === index;
      const numeric = NUMERIC_HINT.test(column);
      headRow.append(el("th", {
        scope: "col",
        class: numeric ? "num" : "",
        "aria-sort": active ? (this.sort.direction === 1 ? "ascending" : "descending") : "none",
        onclick: () => {
          if (this.sort.column === index) this.sort.direction *= -1;
          else this.sort = { column: index, direction: 1 };
          this.#renderTable();
        },
      }, [
        document.createTextNode(column.replace(/_/g, " ")),
        active ? el("span", { class: "sort", text: this.sort.direction === 1 ? "▲" : "▼" }) : null,
      ]));
    });
    thead.append(headRow);

    const tbody = el("tbody");
    for (const row of rows) {
      const tr = el("tr");
      columns.forEach((column, index) => {
        const value = row[index];
        const numeric = NUMERIC_HINT.test(column);
        const text = column === "scale" ? (fmtScale(value) || "—") : fmtNumber(value);
        tr.append(el("td", {
          class: [numeric ? "num" : "", column === "value" ? "mono" : ""].filter(Boolean).join(" "),
          text,
        }));
      });
      tbody.append(tr);
    }

    table.append(thead, tbody);
    host.append(
      el("div", { class: "table-wrap" }, table),
      el("p", {
        class: "panel__sub",
        style: "margin-top:var(--sp-3)",
        text: rows.length === total
          ? plural(total, "row")
          : `${rows.length} of ${plural(total, "row")} shown · ${total - rows.length} filtered out`,
      }),
    );
  }

  #downloadCsv() {
    const data = this.result;
    if (!data?.rows?.length) {
      toast("run a report first", "bad");
      return;
    }
    const cell = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
    const text = [data.columns.map(cell).join(",")]
      .concat(data.rows.map((row) => row.map(cell).join(",")))
      .join("\r\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
    const link = el("a", { href: url, download: `${(data.title || "report").replace(/[^\w.-]+/g, "_")}.csv` });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("report downloaded as CSV", "ok");
  }
}

const ICON_TABLE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 10h17M9.5 10v9.5"/></svg>`;
