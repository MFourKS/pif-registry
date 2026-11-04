/* Поведение сайта. Без этого файла всё содержимое остаётся доступным: таблицы и графики приходят в HTML. */
(() => {
  "use strict";

  /* Рейтинг: фильтры и сортировка живут в адресной строке, поэтому ссылкой можно поделиться. */
  const form = document.getElementById("filters");
  const table = document.getElementById("rating");
  if (form && table) {
    const body = table.tBodies[0];
    const rows = Array.from(body.rows);
    const count = document.getElementById("count");
    const empty = document.getElementById("empty");
    const csvLink = document.getElementById("csv");
    const sortButtons = Array.from(table.querySelectorAll("[data-sort]"));
    const fields = ["q", "type", "asset", "cat", "uk", "cur"];
    const checks = ["retail", "data"];
    const state = { sort: "nav", dir: "desc" };

    const readUrl = () => {
      const params = new URLSearchParams(location.search);
      fields.forEach((name) => { form.elements[name].value = params.get(name) || ""; });
      checks.forEach((name) => { form.elements[name].checked = params.get(name) === "1"; });
      state.sort = params.get("sort") || "nav";
      state.dir = params.get("dir") === "asc" ? "asc" : "desc";
    };

    const writeUrl = () => {
      const params = new URLSearchParams();
      fields.forEach((name) => {
        const value = form.elements[name].value.trim();
        if (value) params.set(name, value);
      });
      checks.forEach((name) => { if (form.elements[name].checked) params.set(name, "1"); });
      if (state.sort !== "nav") params.set("sort", state.sort);
      if (state.dir !== "desc") params.set("dir", state.dir);
      const query = params.toString();
      history.replaceState(null, "", query ? `?${query}` : location.pathname);
    };

    const matches = (row) => {
      const data = row.dataset;
      const words = form.elements.q.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      if (!words.every((word) => data.search.includes(word))) return false;
      if (form.elements.type.value && data.type !== form.elements.type.value) return false;
      if (form.elements.asset.value && data.asset !== form.elements.asset.value) return false;
      if (form.elements.cat.value && data.cat !== form.elements.cat.value) return false;
      if (form.elements.uk.value && data.uk !== form.elements.uk.value) return false;
      if (form.elements.cur.value && data.cur !== form.elements.cur.value) return false;
      if (form.elements.retail.checked && data.qual === "1") return false;
      if (form.elements.data.checked && data.has !== "1") return false;
      return true;
    };

    const value = (row) => {
      const raw = row.dataset[state.sort];
      return raw === "" || raw === undefined ? null : Number(raw);
    };

    const apply = () => {
      const direction = state.dir === "asc" ? 1 : -1;
      const sorted = rows.slice().sort((a, b) => {
        const left = value(a);
        const right = value(b);
        // Пустые значения всегда внизу: «нет данных» не равно нулю.
        if (left === null && right === null) return 0;
        if (left === null) return 1;
        if (right === null) return -1;
        return (left - right) * direction;
      });
      let visible = 0;
      const fragment = document.createDocumentFragment();
      sorted.forEach((row) => {
        const show = matches(row);
        row.hidden = !show;
        if (show) visible += 1;
        fragment.appendChild(row);
      });
      body.appendChild(fragment);
      count.textContent = `${visible} из ${rows.length} фондов`;
      empty.hidden = visible > 0;
      sortButtons.forEach((button) => {
        const active = button.dataset.sort === state.sort;
        button.closest("th").setAttribute("aria-sort", active ? (state.dir === "asc" ? "ascending" : "descending") : "none");
      });
    };

    /* CSV выгружает то, что видно на экране, поэтому берётся текущая выборка. */
    const exportVisible = async (event) => {
      event.preventDefault();
      const response = await fetch(csvLink.getAttribute("href"));
      const text = (await response.text()).replace(/^﻿/, "");
      const lines = text.split("\n");
      const header = lines.shift();
      const byRule = new Map(lines.filter(Boolean).map((line) => [line.split(",")[0], line]));
      const chosen = Array.from(body.rows)
        .filter((row) => !row.hidden)
        .map((row) => byRule.get(decodeURIComponent(row.querySelector("a").getAttribute("href").split("/").filter(Boolean).pop())))
        .filter(Boolean);
      const blob = new Blob(["﻿", [header, ...chosen].join("\n"), "\n"], { type: "text/csv;charset=utf-8" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "pif-rating.csv";
      link.click();
      URL.revokeObjectURL(link.href);
    };

    readUrl();
    apply();
    form.addEventListener("input", () => { writeUrl(); apply(); });
    form.addEventListener("submit", (event) => event.preventDefault());
    csvLink.addEventListener("click", exportVisible);
    document.getElementById("reset").addEventListener("click", () => {
      form.reset();
      state.sort = "nav";
      state.dir = "desc";
      writeUrl();
      apply();
    });
    sortButtons.forEach((button) => button.addEventListener("click", () => {
      const key = button.dataset.sort;
      state.dir = state.sort === key && state.dir === "desc" ? "asc" : "desc";
      state.sort = key;
      writeUrl();
      apply();
    }));
    window.addEventListener("popstate", () => { readUrl(); apply(); });
  }

  /* Карточка фонда: вкладки графиков и подсказка с источником любой точки. */
  const panel = document.querySelector("[data-fund]");
  if (panel) {
    const tabs = Array.from(panel.querySelectorAll("[data-tab]"));
    const panes = Array.from(panel.querySelectorAll("[data-pane]"));
    const tip = panel.querySelector("#tip");
    let data = null;

    tabs.forEach((tab) => tab.addEventListener("click", () => {
      tabs.forEach((other) => other.setAttribute("aria-selected", String(other === tab)));
      panes.forEach((pane) => { pane.hidden = pane.dataset.pane !== tab.dataset.tab; });
      tip.hidden = true;
    }));

    const fmt = (number, digits = 2) => (number === null || number === undefined
      ? "—"
      : number.toLocaleString("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits }));

    const load = async () => {
      if (data) return data;
      const response = await fetch(panel.dataset.fund);
      data = await response.json();
      return data;
    };

    const showPoint = async (svg, event) => {
      const payload = await load();
      if (!payload.points.length) return;
      const box = svg.getBoundingClientRect();
      const x0 = Number(svg.dataset.x0);
      const x1 = Number(svg.dataset.x1);
      const scale = svg.viewBox.baseVal.width / box.width;
      const ratio = Math.min(1, Math.max(0, ((event.clientX - box.left) * scale - x0) / (x1 - x0)));
      const first = Date.parse(payload.points[0][0]);
      const last = Date.parse(payload.points[payload.points.length - 1][0]);
      const target = first + (last - first) * ratio;
      let best = payload.points[0];
      payload.points.forEach((point) => {
        if (Math.abs(Date.parse(point[0]) - target) < Math.abs(Date.parse(best[0]) - target)) best = point;
      });
      const [day, nav, price, , sourceIndex, monthly] = best;
      const source = payload.sources[sourceIndex] || {};
      tip.hidden = false;
      tip.replaceChildren();
      const label = day.split("-").reverse().join(".");
      tip.append(`${label}: СЧА ${fmt(nav)}, пай ${fmt(price, 4)} ${payload.currency}${monthly ? " (КИД)" : ""}. `);
      if (source.url) {
        const link = document.createElement("a");
        link.href = source.url;
        link.rel = "nofollow noopener";
        link.textContent = `Источник (${source.fmt}, получен ${source.date})`;
        tip.append(link);
      }
    };

    panel.querySelectorAll("svg[data-x0]").forEach((svg) => {
      svg.addEventListener("pointermove", (event) => showPoint(svg, event));
      svg.addEventListener("click", (event) => showPoint(svg, event));
    });
  }
})();
