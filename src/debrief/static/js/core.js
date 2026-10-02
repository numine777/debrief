/* Debrief viewer core: DOM helpers, data access, Markdown, highlighting, glyphs. */
(function () {
  "use strict";
  const D = (window.Debrief = window.Debrief || {});

  // --- DOM -------------------------------------------------------------------------
  function h(tag, attrs, ...children) {
    const svgTags = h.svgTags;
    const el = svgTags.has(tag) ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") el.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
        else if (key === "dataset") Object.assign(el.dataset, value);
        else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
        else if (key === "html") el.innerHTML = value; // only ever given sanitized or escaped HTML
        else if (value === true) el.setAttribute(key, "");
        else el.setAttribute(key, String(value));
      }
    }
    append(el, children);
    return el;
  }
  h.svgTags = new Set(["svg", "path", "rect", "circle", "g", "text", "line", "polygon", "title", "defs", "marker", "tspan"]);

  function append(el, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) append(el, child);
      else if (child instanceof Node) el.appendChild(child);
      else el.appendChild(document.createTextNode(String(child)));
    }
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // --- data -------------------------------------------------------------------------
  const exported = window.DEBRIEF_EXPORT || null;
  const cache = new Map();

  async function api(path, options) {
    options = options || {};
    if (exported) {
      const key = path.replace(/^\/api\/v1/, "");
      if (key in exported.routes) return exported.routes[key];
      throw new ApiError(404, "Not included in this export.");
    }
    const init = { method: options.method || "GET", headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (init.method !== "GET") init.headers["X-Debrief"] = "1";
    const resp = await fetch(path, init);
    const type = resp.headers.get("Content-Type") || "";
    const data = type.includes("json") ? await resp.json() : await resp.text();
    if (!resp.ok) throw new ApiError(resp.status, (data && data.error) || resp.statusText);
    return data;
  }

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function cached(key, loader) {
    if (cache.has(key)) return cache.get(key);
    const promise = loader().catch((err) => { cache.delete(key); throw err; });
    cache.set(key, promise);
    return promise;
  }

  function invalidate(prefix) {
    for (const key of Array.from(cache.keys())) if (!prefix || key.startsWith(prefix)) cache.delete(key);
  }

  const featurePath = (pid, fid) => `/api/v1/projects/${encodeURIComponent(pid)}/features/${encodeURIComponent(fid)}`;
  const data = {
    meta: () => cached("meta", () => api("/api/v1/meta")),
    index: () => cached("index", () => api("/api/v1/index")),
    feature: (pid, fid) => cached(`f:${pid}/${fid}`, () => api(featurePath(pid, fid))),
    diff: (pid, fid, scope) => cached(`d:${pid}/${fid}:${scope}`, () =>
      api(`${featurePath(pid, fid)}/diff?scope=${encodeURIComponent(scope)}`)),
    blob: (pid, fid, blob) => cached(`b:${pid}/${fid}:${blob}`, async () => {
      if (exported) {
        const text = exported.blobs && exported.blobs[blob];
        if (text === undefined) throw new ApiError(404, "This export doesn't include that file version.");
        return text;
      }
      const resp = await fetch(`${featurePath(pid, fid)}/blobs/${blob}`, { credentials: "same-origin" });
      if (!resp.ok) throw new ApiError(resp.status, "That file version is not archived.");
      return resp.text();
    }),
    commit: (sha) => cached(`c:${sha}`, () => api(`/api/v1/commits/${encodeURIComponent(sha)}`)),
    search: (q) => api(`/api/v1/search?q=${encodeURIComponent(q)}`),
    epic: (name) => cached(`e:${name}`, () => api(`/api/v1/epics/${encodeURIComponent(name)}`)),
    post: (path, body) => api(path, { method: "POST", body: body || {} }),
    patch: (path, body) => api(path, { method: "PATCH", body: body || {} }),
    del: (path) => api(path, { method: "DELETE" }),
    featurePath,
  };

  // --- Markdown (agent-written, untrusted) ---------------------------------------------------
  let markedReady = false;
  function setupMarked() {
    if (markedReady || !window.marked) return;
    window.marked.use({
      gfm: true,
      breaks: false,
      renderer: { html() { return ""; } }, // raw HTML in records is dropped, never rendered
    });
    if (window.DOMPurify) {
      window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
        if (node.tagName === "A") {
          const href = node.getAttribute("href") || "";
          if (/^https?:/i.test(href)) { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
          else if (!href.startsWith("#")) node.removeAttribute("href");
        }
      });
    }
    markedReady = true;
  }

  function markdown(text, opts) {
    setupMarked();
    const src = String(text || "");
    let html;
    if (window.marked) html = window.marked.parse(src);
    else html = "<p>" + escapeHtml(src).replace(/\n\n+/g, "</p><p>") + "</p>";
    if (window.DOMPurify) {
      html = window.DOMPurify.sanitize(html, { FORBID_TAGS: ["style", "form", "input", "iframe"], FORBID_ATTR: ["style"] });
    }
    const div = h("div", { class: ["prose", opts && opts.class] });
    div.innerHTML = html;
    if (opts && opts.linkCode) opts.linkCode(div);
    return div;
  }

  // --- syntax highlighting ----------------------------------------------------------------------
  const LANGS = {
    py: "python", pyi: "python", bzl: "python", star: "python", bazel: "python", js: "javascript", mjs: "javascript",
    cjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", go: "go", rs: "rust", java: "java",
    kt: "kotlin", kts: "kotlin", scala: "scala", cs: "csharp", swift: "swift", c: "c", h: "c", cc: "cpp", cpp: "cpp",
    cxx: "cpp", hpp: "cpp", hh: "cpp", m: "objectivec", mm: "objectivec", rb: "ruby", php: "php", sh: "bash",
    bash: "bash", zsh: "bash", json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", cfg: "ini",
    md: "markdown", css: "css", scss: "scss", html: "xml", xml: "xml", svg: "xml", sql: "sql", proto: "protobuf",
    dockerfile: "dockerfile", mk: "makefile", lua: "lua", r: "r", dart: "dart", vue: "xml", gradle: "gradle",
  };
  const NAMES = { BUILD: "python", WORKSPACE: "python", Makefile: "makefile", Dockerfile: "dockerfile", "CMakeLists.txt": "cmake" };

  function languageFor(path) {
    const name = String(path || "").split("/").pop();
    if (NAMES[name]) return NAMES[name];
    if (name.startsWith("BUILD.")) return "python";
    const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    const lang = LANGS[ext];
    return lang && window.hljs && window.hljs.getLanguage(lang) ? lang : null;
  }

  function highlightLine(text, lang) {
    if (!lang || !window.hljs || text.length > 2000) return escapeHtml(text);
    try {
      return window.hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
    } catch (err) {
      return escapeHtml(text);
    }
  }

  // --- glyphs: coverage states as map-symbol frames ------------------------------------------------
  const STATE = {
    covered: { label: "explained", color: "var(--friend)", wash: "var(--friend-wash)" },
    incidental: { label: "incidental", color: "var(--neutral)", wash: "var(--neutral-wash)" },
    weak: { label: "weak claim", color: "var(--unknown)", wash: "var(--unknown-wash)" },
    unclaimed: { label: "unexplained", color: "var(--hostile)", wash: "var(--hostile-wash)" },
    superseded: { label: "replaced later", color: "var(--superseded)", wash: "transparent" },
    none: { label: "not analyzed yet", color: "var(--rule-strong)", wash: "transparent" },
  };

  function glyph(state, title) {
    const s = STATE[state] || STATE.weak;
    const svg = h("svg", { class: "glyph", viewBox: "0 0 14 12", "aria-hidden": title ? null : "true", role: title ? "img" : null });
    if (title) svg.appendChild(h("title", null, title));
    const style = { fill: s.wash, stroke: s.color, "stroke-width": "1.6" };
    let shape;
    if (state === "covered") shape = h("rect", Object.assign({ x: 1, y: 2.5, width: 12, height: 7 }, style));
    else if (state === "incidental") shape = h("rect", Object.assign({ x: 2.5, y: 1.5, width: 9, height: 9 }, style));
    else if (state === "unclaimed") shape = h("polygon", Object.assign({ points: "7,0.8 12.6,6 7,11.2 1.4,6" }, style));
    else if (state === "superseded") shape = h("rect", Object.assign({ x: 1, y: 2.5, width: 12, height: 7, "stroke-dasharray": "2 1.5" }, style));
    else shape = h("path", Object.assign({ d: "M7 1.2a2.6 2.6 0 0 1 2.6 2.4A2.6 2.6 0 0 1 12 6a2.6 2.6 0 0 1-2.4 2.4A2.6 2.6 0 0 1 7 10.8a2.6 2.6 0 0 1-2.6-2.4A2.6 2.6 0 0 1 2 6a2.6 2.6 0 0 1 2.4-2.4A2.6 2.6 0 0 1 7 1.2z" }, style));
    svg.appendChild(shape);
    return svg;
  }

  function stateLabel(state) { return (STATE[state] || { label: state }).label; }

  const ICONS = {
    search: "M10.5 10.5 14 14M6.8 12a5.2 5.2 0 1 1 0-10.4 5.2 5.2 0 0 1 0 10.4z",
    theme: "M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13zm0 0v13",
    help: "M6 6a2 2 0 1 1 3 1.7c-.6.4-1 .8-1 1.5V10M8 12.5v.5",
    refresh: "M13 3v3.5H9.5M3 13V9.5h3.5M12.6 6.5A5 5 0 0 0 3.6 5M3.4 9.5a5 5 0 0 0 9 1.5",
  };
  function icon(name) {
    return h("svg", { viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "aria-hidden": "true" },
      h("path", { d: ICONS[name] }));
  }

  function mark() {
    // The wordmark: a hunk frame with a check, the act of explaining a change.
    return h("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" },
      h("rect", { x: 2, y: 5, width: 20, height: 14, rx: 1.5, fill: "var(--friend-wash)", stroke: "var(--ink)", "stroke-width": "2" }),
      h("path", { d: "M7 12.2l3.2 3L17 8.8", fill: "none", stroke: "var(--friend)", "stroke-width": "2.4", "stroke-linecap": "round", "stroke-linejoin": "round" }));
  }

  // --- coverage strip -------------------------------------------------------------------------------
  const tooltip = h("div", { class: "tooltip", role: "tooltip", hidden: true });
  function showTip(evt, content) {
    if (!tooltip.isConnected) document.body.appendChild(tooltip);
    clear(tooltip);
    append(tooltip, [content]);
    tooltip.hidden = false;
    const x = Math.min(evt.clientX + 12, window.innerWidth - 300);
    const y = evt.clientY + 16;
    tooltip.style.left = x + "px";
    tooltip.style.top = y + "px";
  }
  function hideTip() { tooltip.hidden = true; }

  function units(files) {
    const out = [];
    for (const f of files || []) {
      const hunks = f.hunks && f.hunks.length ? f.hunks : [];
      for (const hk of hunks) {
        out.push({ path: f.path, id: hk.id, state: hk.state || "unclaimed", size: (hk.additions || 0) + (hk.deletions || 0) || 1,
          symbol: hk.symbol, line: hk.new_start || hk.old_start, noise: f.noise });
      }
    }
    return out;
  }

  function coverageStrip(files, opts) {
    opts = opts || {};
    const list = units(files);
    const strip = h("div", { class: ["strip", opts.mini && "mini"], role: "group",
      "aria-label": opts.label || "Coverage of changed hunks" });
    let lastPath = null;
    for (const u of list) {
      if (lastPath !== null && u.path !== lastPath) strip.appendChild(h("span", { class: "file-break", "aria-hidden": "true" }));
      lastPath = u.path;
      const weight = Math.max(1, Math.round(Math.sqrt(u.size) * 10));
      const reviewed = opts.reviewed && opts.reviewed.has(u.id);
      const cell = h(opts.mini ? "span" : "button", {
        class: ["cell", "state-" + u.state, reviewed && "reviewed"],
        type: opts.mini ? null : "button",
        "aria-label": opts.mini ? null : `${u.path}${u.symbol ? " " + u.symbol : ""}: ${u.size} changed lines, ${stateLabel(u.state)}`,
      });
      cell.style.flexGrow = String(weight);
      if (!opts.mini) {
        cell.addEventListener("mouseenter", (evt) => showTip(evt, h("div", null,
          h("div", { class: "mono" }, u.path + (u.line ? ":" + u.line : "")),
          h("div", null, `${u.symbol ? u.symbol + " · " : ""}${u.size} lines · ${stateLabel(u.state)}${reviewed ? " · reviewed" : ""}`))));
        cell.addEventListener("mouseleave", hideTip);
        cell.addEventListener("click", () => { hideTip(); if (opts.onPick) opts.onPick(u); });
      }
      strip.appendChild(cell);
    }
    if (!list.length) strip.appendChild(h("span", { class: "muted small" }, opts.mini ? "" : "No changes yet"));
    return strip;
  }

  function coverageLegend(cov) {
    cov = cov || {};
    const item = (state, n) => h("span", null, glyph(state), h("b", null, String(n || 0)), stateLabel(state));
    return h("div", { class: "legend" },
      item("covered", cov.covered), item("incidental", cov.incidental), item("weak", cov.weak), item("unclaimed", cov.unclaimed));
  }

  // --- time and numbers ----------------------------------------------------------------------------------
  function parseTime(value) {
    if (!value) return null;
    let text = String(value);
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$/.test(text)) text = text.replace("Z", ":00Z");
    const t = Date.parse(text);
    return Number.isNaN(t) ? null : new Date(t);
  }

  function ago(value) {
    const d = parseTime(value);
    if (!d) return "";
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 45) return "just now";
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min ago`;
    const hrs = Math.round(m / 60);
    if (hrs < 36) return `${hrs} h ago`;
    const days = Math.round(hrs / 24);
    if (days < 21) return `${days} days ago`;
    return d.toISOString().slice(0, 10);
  }

  function clock(value) {
    const d = parseTime(value);
    if (!d) return value || "";
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function pct(ratio) { return typeof ratio === "number" ? Math.round(ratio * 100) + "%" : "n/a"; }
  // Split git trailers ("Key: value" lines at the end of a message) from the body.
  function splitTrailers(body) {
    const lines = String(body || "").replace(/\s+$/, "").split("\n");
    let i = lines.length;
    while (i > 0 && /^[A-Za-z][A-Za-z0-9-]*: \S/.test(lines[i - 1])) i--;
    if (i === lines.length || (i > 0 && lines[i - 1].trim() !== "")) return { text: lines.join("\n"), trailers: [] };
    return { text: lines.slice(0, i).join("\n").trim(), trailers: lines.slice(i) };
  }

  function plural(n, word, many) { return `${n} ${n === 1 ? word : (many || word + "s")}`; }
  function short(sha) { return sha ? String(sha).slice(0, 9) : ""; }

  // --- small UI pieces --------------------------------------------------------------------------------
  function toast(message) {
    const el = h("div", { class: "toast", role: "status" }, message);
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2600);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied to the clipboard");
      return true;
    } catch (err) {
      toast("Copying needs a secure context; use Download instead");
      return false;
    }
  }

  function download(name, text, type) {
    const url = URL.createObjectURL(new Blob([text], { type: type || "text/markdown" }));
    const a = h("a", { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function errorBox(err) {
    const status = err && err.status;
    const msg = err && err.message ? err.message : String(err);
    return h("div", { class: "error-box", role: "alert" },
      h("strong", null, status === 404 ? "Not found. " : "Something went wrong. "), msg);
  }

  function prefs() {
    try { return JSON.parse(localStorage.getItem("debrief.prefs") || "{}"); } catch (err) { return {}; }
  }
  function savePref(key, value) {
    try {
      const p = prefs();
      p[key] = value;
      localStorage.setItem("debrief.prefs", JSON.stringify(p));
    } catch (err) { /* storage unavailable: keep the in-memory choice */ }
  }

  Object.assign(D, {
    h, append, clear, escapeHtml, api, ApiError, data, invalidate, markdown, languageFor, highlightLine, glyph,
    stateLabel, icon, mark, coverageStrip, coverageLegend, units, showTip, hideTip, parseTime, ago, clock, pct,
    plural, short, splitTrailers, toast, copyText, download, errorBox, prefs, savePref, exported, STATE,
  });
})();
