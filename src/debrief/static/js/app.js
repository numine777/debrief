/* Debrief viewer: shell, routing, theme and keyboard. */
(function () {
  "use strict";
  const D = window.Debrief;
  const { h, clear } = D;

  // --- routing -------------------------------------------------------------------------------------
  function parseRoute() {
    const raw = location.hash.replace(/^#/, "") || "/";
    const second = raw.indexOf("#", 1);
    const target = second >= 0 ? raw.slice(0, second) : raw;
    const [pathPart, queryPart] = target.split("?");
    const query = {};
    for (const [k, v] of new URLSearchParams(queryPart || "")) query[k] = v;
    const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
    if (!parts.length) return { name: "index", query };
    if (parts[0] === "p" && parts[2] === "f" && parts.length >= 4) {
      return { name: "feature", pid: parts[1], fid: parts[3], tab: parts[4] || "brief", sub: parts[5], query };
    }
    if (parts[0] === "commit" && parts[1]) return { name: "commit", sha: parts[1], query };
    if (parts[0] === "search") return { name: "search", query };
    if (parts[0] === "epic" && parts[1]) return { name: "epic", name_: parts[1], query, epicName: parts[1] };
    return { name: "missing", query };
  }

  let main, crumbs, renderToken = 0;

  async function render() {
    const route = parseRoute();
    const token = ++renderToken;
    D.hideTip();
    D.setCrumbs([]);
    window.scrollTo(0, 0);
    try {
      if (route.name === "index") await D.viewIndex(main);
      else if (route.name === "feature") await D.viewFeature(main, route);
      else if (route.name === "commit") await D.viewCommit(main, route);
      else if (route.name === "search") await D.viewSearch(main, route);
      else if (route.name === "epic") await D.viewEpic(main, { name: route.epicName });
      else clear(main).appendChild(D.errorBox({ status: 404, message: "That page doesn't exist. Go back to the feature list." }));
    } catch (err) {
      if (token !== renderToken) return;
      clear(main).appendChild(D.errorBox(err));
      if (window.console) console.error(err);
    }
    if (token === renderToken) document.title = pageTitle();
  }

  function pageTitle() {
    const h1 = main.querySelector("h1");
    return h1 ? `${h1.textContent} · Debrief` : "Debrief";
  }

  D.rerender = render;
  D.route = parseRoute;

  D.setCrumbs = function (items) {
    if (!crumbs) return;
    clear(crumbs);
    items.forEach(([label, href], i) => {
      if (i) crumbs.appendChild(h("span", { class: "sep", "aria-hidden": "true" }, "/"));
      crumbs.appendChild(href ? h("a", { href }, label) : h("span", null, label));
    });
  };

  // --- theme ------------------------------------------------------------------------------------------
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.setAttribute("data-theme", theme);
    else document.documentElement.removeAttribute("data-theme");
  }
  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") ||
      (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    D.savePref("theme", next);
  }

  // --- keyboard ----------------------------------------------------------------------------------------
  let pendingG = false;
  function moveHunk(step) {
    const hunks = Array.from(document.querySelectorAll(".hunk"));
    if (!hunks.length) return;
    const current = document.querySelector(".hunk.current");
    let i = current ? hunks.indexOf(current) + step : (step > 0 ? 0 : hunks.length - 1);
    if (!current) {
      // start from the first hunk below the top of the viewport
      const top = 80;
      i = hunks.findIndex((el) => el.getBoundingClientRect().top >= top);
      if (i < 0) i = hunks.length - 1;
      if (step < 0) i = Math.max(0, i - 1);
    }
    i = Math.max(0, Math.min(hunks.length - 1, i));
    if (current) current.classList.remove("current");
    hunks[i].classList.add("current");
    hunks[i].scrollIntoView({ block: "start" });
    hunks[i].focus({ preventScroll: true });
  }
  function moveCard(step) {
    const cards = Array.from(document.querySelectorAll(".card, .system-group, .file"));
    if (!cards.length) return;
    const top = 90;
    let i = cards.findIndex((el) => el.getBoundingClientRect().top > top + 2);
    if (i < 0) i = cards.length;
    i = step > 0 ? i : i - 2;
    i = Math.max(0, Math.min(cards.length - 1, i));
    cards[i].scrollIntoView({ block: "start" });
  }

  function featureBase() {
    const r = parseRoute();
    return r.name === "feature" ? `#/p/${encodeURIComponent(r.pid)}/f/${encodeURIComponent(r.fid)}` : null;
  }

  function onKey(evt) {
    if (evt.defaultPrevented || evt.metaKey || evt.ctrlKey || evt.altKey) return;
    const tag = (evt.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag) || evt.target.isContentEditable) {
      if (evt.key === "Escape") evt.target.blur();
      return;
    }
    if (document.querySelector("dialog[open]")) return;
    const base = featureBase();
    if (pendingG) {
      pendingG = false;
      const go = { i: "#/", b: base, m: base && base + "/map", s: base && base + "/systems", t: base && base + "/tests",
        q: base && base + "/queue", d: base && base + "/diff", l: base && base + "/timeline" }[evt.key];
      if (go) { location.hash = go; evt.preventDefault(); }
      return;
    }
    switch (evt.key) {
      case "g": pendingG = true; setTimeout(() => { pendingG = false; }, 1200); break;
      case "j": moveHunk(1); break;
      case "k": moveHunk(-1); break;
      case "n": moveCard(1); break;
      case "p": moveCard(-1); break;
      case "/": evt.preventDefault(); { const s = document.querySelector(".searchbox input"); if (s) s.focus(); } break;
      case "t": toggleTheme(); break;
      case "?": showHelp(); break;
      default:
        if (D.onKey) D.onKey(evt);
        return;
    }
  }

  function showHelp() {
    const keys = [["j / k", "Next or previous hunk"], ["n / p", "Next or previous commit, system or file"], ["/", "Search"],
      ["g then i", "Feature list"], ["g then b, m, s, t, q, d, l", "Brief, map, systems, tests, queue, diff, timeline"],
      ["t", "Switch light and dark"], ["?", "This help"]].concat(D.extraKeys || []);
    const dlg = h("dialog", { "aria-labelledby": "help-title" },
      h("h2", { id: "help-title" }, "Keyboard"),
      h("div", { class: "keys" }, keys.map(([k, v]) => [h("span", null, k.split(" ").map((p) => (p === "/" && k.length > 1) || p === "then" || p === "or" ? ` ${p} ` : h("kbd", null, p))), h("span", null, v)])),
      h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "Close")));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.appendChild(dlg);
    dlg.showModal();
  }

  // --- boot --------------------------------------------------------------------------------------------
  function shell() {
    const app = document.getElementById("app");
    crumbs = h("div", { class: "crumbs" });
    const search = h("input", { type: "search", placeholder: "Search", "aria-label": "Search systems, files, symbols and journals" });
    const searchForm = h("form", { class: "searchbox", role: "search", onsubmit: (evt) => {
      evt.preventDefault();
      if (search.value.trim()) location.hash = `#/search?q=${encodeURIComponent(search.value.trim())}`;
    } }, D.icon("search"), search);
    const top = h("header", { class: "topbar" },
      h("a", { class: "wordmark", href: "#/" }, D.mark(), "Debrief"),
      crumbs, h("span", { class: "spacer" }),
      D.exported ? h("span", { class: "muted small" }, "Exported copy, read-only") : searchForm,
      h("button", { class: "iconbtn", type: "button", title: "Switch light and dark (t)", "aria-label": "Switch light and dark", onclick: toggleTheme }, D.icon("theme")),
      h("button", { class: "iconbtn", type: "button", title: "Keyboard shortcuts (?)", "aria-label": "Keyboard shortcuts", onclick: showHelp }, D.icon("help")));
    main = h("main", { id: "main", tabindex: "-1" });
    clear(app);
    app.appendChild(h("a", { class: "visually-hidden", href: "#main" }, "Skip to content"));
    app.appendChild(top);
    app.appendChild(main);
  }

  function boot() {
    applyTheme(D.prefs().theme);
    shell();
    window.addEventListener("hashchange", render);
    document.addEventListener("keydown", onKey);
    if (D.exported && D.exported.start && (!location.hash || location.hash === "#/")) location.hash = D.exported.start;
    else render();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
