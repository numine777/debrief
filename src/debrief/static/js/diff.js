/* Debrief viewer: diff rendering with coverage state, claims, invariants and context. */
(function () {
  "use strict";
  const D = window.Debrief;
  const { h, clear, glyph, stateLabel, highlightLine, languageFor } = D;

  const MAX_INITIAL_LINES = 160;
  const CONTEXT_STEP = 12;

  function claimSystems(hunk) {
    if (hunk.systems) return hunk.systems;
    const out = [];
    for (const c of hunk.claims || []) if (c.kind === "system" && !out.includes(c.by)) out.push(c.by);
    return out;
  }

  function claimTests(hunk) {
    const out = new Set(hunk.tests || []);
    for (const c of hunk.claims || []) if (c.kind === "test") out.add(c.by);
    return Array.from(out);
  }

  function featureLinks(ctx) {
    const base = `#/p/${encodeURIComponent(ctx.pid)}/f/${encodeURIComponent(ctx.fid)}`;
    return {
      system: (sid) => `${base}/system/${encodeURIComponent(sid)}`,
      tests: () => `${base}/tests`,
      base,
    };
  }

  function testStatusIndex(feature) {
    const out = {};
    for (const t of (feature && feature.evidence && feature.evidence.tests) || []) out[t.id] = t;
    return out;
  }

  const TEST_LABEL = {
    verified_pass: "verified pass", verified_fail: "verified fail", claimed_only: "claimed only",
    claimed_fail: "claimed fail", not_run: "not run", manual: "manual",
  };

  function testBadge(status) {
    const label = TEST_LABEL[status] || status || "unknown";
    const mark = status === "verified_pass" ? "✓" : status === "verified_fail" ? "✗" : status === "claimed_only" ? "?" : "·";
    return h("span", { class: `test-status ts-${status}` }, mark, " ", label);
  }

  function criticalPathIndex(feature) {
    const out = {};
    for (const s of (feature && feature.systems) || []) {
      for (const cp of s.critical_paths || []) out[`${s.id}/${cp.id}`] = Object.assign({ system: s.id }, cp);
    }
    return out;
  }

  function flagIndex(feature) {
    // queue items carry hunk ids and lines; index flags by hunk for line marks and callouts
    const out = {};
    for (const item of (feature && feature.evidence && feature.evidence.queue) || []) {
      if (!item.hunk_id || !["undeclared-critical", "risky", "weakened-test"].includes(item.kind)) continue;
      (out[item.hunk_id] = out[item.hunk_id] || []).push(item);
    }
    return out;
  }

  function lineRow(tag, oldNo, newNo, text, lang, extra) {
    const cls = tag === "+" ? "add" : tag === "-" ? "del" : tag === "\\" ? "note" : "ctx";
    const row = h("tr", { class: [cls, extra && extra.cls], dataset: { side: tag === "-" ? "old" : "new", line: String(tag === "-" ? oldNo : newNo || "") } },
      h("td", { class: "ln" }, oldNo === null || oldNo === undefined ? "" : String(oldNo)),
      h("td", { class: "ln" }, newNo === null || newNo === undefined ? "" : String(newNo)),
      h("td", { class: "sign" }, tag === " " ? "" : tag === "\\" ? "" : tag),
      h("td", { class: "src", html: tag === "\\" ? D.escapeHtml(text) : highlightLine(text, lang) }));
    if (extra && extra.title) row.title = extra.title;
    return row;
  }

  function numbered(hunk) {
    const rows = [];
    let o = hunk.old_start, n = hunk.new_start;
    for (const [tag, text] of hunk.lines || []) {
      if (tag === " ") { rows.push([tag, o, n, text]); o++; n++; }
      else if (tag === "-") { rows.push([tag, o, null, text]); o++; }
      else if (tag === "+") { rows.push([tag, null, n, text]); n++; }
      else rows.push([tag, null, null, text]);
    }
    return rows;
  }

  function renderHunk(file, hunk, ctx) {
    const links = featureLinks(ctx);
    const lang = ctx.noHighlight ? null : languageFor(file.path);
    const state = hunk.state || "none";
    const systems = claimSystems(hunk);
    const tests = claimTests(hunk);
    const testIdx = ctx.testIndex || testStatusIndex(ctx.feature);
    const flags = (ctx.flagIndex || {})[hunk.id] || [];
    const flaggedLines = new Set(flags.filter((f) => f.side !== "old").map((f) => f.line));
    const el = h("section", { class: ["hunk", "state-" + state], id: ctx.idPrefix ? ctx.idPrefix + hunk.id : "hunk-" + hunk.id,
      dataset: { hunk: hunk.id, path: file.path }, tabindex: "-1" });

    const head = h("div", { class: "hunk-head" },
      h("span", { class: "state-label" }, glyph(state), stateLabel(state)),
      h("span", { class: "loc" }, `@@ -${hunk.old_start},${hunk.old_len} +${hunk.new_start},${hunk.new_len}`),
      hunk.symbol || hunk.section ? h("span", { class: "sym" }, hunk.symbol || hunk.section) : null,
      systems.length ? h("span", { class: "chips" }, systems.map((sid) => h("a", { class: "chip system", href: links.system(sid) }, sid))) : null,
      tests.length ? h("span", { class: "chips" }, tests.map((tid) => h("a", { class: "chip test", href: links.tests(), title: (testIdx[tid] && TEST_LABEL[testIdx[tid].status]) || "" }, tid))) : null,
      h("span", { class: "right" }, ctx.hunkActions ? ctx.hunkActions(file, hunk) : null));
    el.appendChild(head);

    const table = h("table", { class: "code" });
    const tbody = h("tbody");
    table.appendChild(tbody);
    const rows = numbered(hunk);
    const appendRows = (from, to) => {
      for (let i = from; i < to; i++) {
        const [tag, o, n, text] = rows[i];
        const flagged = tag === "+" && flaggedLines.has(n);
        const tr = lineRow(tag, o, n, text, lang, flagged ? { cls: "flagged", title: flags.filter((f) => f.line === n).map((f) => f.title).join("\n") } : null);
        const extra = ctx.decorateLine ? ctx.decorateLine(tr, file, hunk, tag, o, n, text) : null;
        tbody.appendChild(tr);
        if (extra) for (const row of extra) tbody.appendChild(row);
      }
    };
    if (rows.length > MAX_INITIAL_LINES * 1.4) {
      appendRows(0, MAX_INITIAL_LINES);
      const more = h("tr", { class: "more" }, h("td", { colspan: 4 },
        h("button", { class: "btn small", type: "button", onclick: () => { more.remove(); appendRows(MAX_INITIAL_LINES, rows.length); } },
          `Show ${rows.length - MAX_INITIAL_LINES} more lines`)));
      tbody.appendChild(more);
    } else {
      appendRows(0, rows.length);
    }

    // Context on demand from the archived file versions.
    const blob = file.status === "D" ? file.old_blob : file.new_blob;
    if (blob && !file.binary && ctx.pid && hunk.lines && hunk.lines.length) {
      const sideStart = file.status === "D" ? hunk.old_start : hunk.new_start;
      const sideLen = file.status === "D" ? hunk.old_len : hunk.new_len;
      let topLoaded = sideStart; // first visible line number on the blob's side
      let bottomLoaded = sideStart + Math.max(sideLen, 0) - 1;
      const above = h("button", { class: "ctx-btn", type: "button" }, "Show lines above");
      const below = h("button", { class: "ctx-btn", type: "button" }, "Show lines below");
      const offsetAbove = hunk.old_start - hunk.new_start;
      const offsetBelow = (hunk.old_start + hunk.old_len) - (hunk.new_start + hunk.new_len);
      const blobLines = () => D.data.blob(ctx.pid, ctx.fid, blob).then((text) => text.split("\n"));
      above.addEventListener("click", async () => {
        try {
          const lines = await blobLines();
          const end = topLoaded - 1;
          const start = Math.max(1, end - CONTEXT_STEP + 1);
          const frag = document.createDocumentFragment();
          for (let n = start; n <= end; n++) {
            const other = file.status === "D" ? null : n + offsetAbove;
            frag.appendChild(lineRow(" ", file.status === "D" ? n : other, file.status === "D" ? null : n, lines[n - 1] || "", lang, { cls: "ctx-extra" }));
          }
          tbody.insertBefore(frag, tbody.firstChild);
          topLoaded = start;
          if (start <= 1) above.remove();
        } catch (err) { D.toast(err.message); above.remove(); }
      });
      below.addEventListener("click", async () => {
        try {
          const lines = await blobLines();
          const start = bottomLoaded + 1;
          const end = Math.min(lines.length - (lines[lines.length - 1] === "" ? 1 : 0), start + CONTEXT_STEP - 1);
          for (let n = start; n <= end; n++) {
            const other = file.status === "D" ? null : n + offsetBelow;
            tbody.appendChild(lineRow(" ", file.status === "D" ? n : other, file.status === "D" ? null : n, lines[n - 1] || "", lang, { cls: "ctx-extra" }));
          }
          bottomLoaded = end;
          if (end >= lines.length - 1) below.remove();
        } catch (err) { D.toast(err.message); below.remove(); }
      });
      const wholeFile = file.status === "A" || file.status === "D";
      if (sideStart > 1 && !wholeFile) el.appendChild(above);
      el.appendChild(table);
      if (!wholeFile) el.appendChild(below);
    } else {
      el.appendChild(table);
    }

    // Callouts: the invariants this hunk touches, the tests behind it, and what was flagged.
    const cps = (hunk.critical_paths || []).map((ref) => (ctx.cpIndex || {})[ref]).filter(Boolean);
    const callouts = [];
    for (const cp of cps) {
      callouts.push(h("div", { class: "callout" }, h("span", { class: "what" }, "Invariant"),
        h("span", null, h("a", { href: links.system(cp.system), class: "mono small" }, `${cp.system}/${cp.id}`), " ",
          h("span", { class: "inv" }, cp.invariant || "(none given)"))));
    }
    if (tests.length) {
      callouts.push(h("div", { class: "callout" }, h("span", { class: "what" }, "Tests"),
        h("span", { class: "chips" }, tests.map((tid) => h("span", null, h("a", { href: links.tests(), class: "mono small" }, tid), " ",
          testIdx[tid] ? testBadge(testIdx[tid].status) : null)))));
    }
    for (const f of flags) {
      callouts.push(h("div", { class: "callout" }, h("span", { class: "what" }, f.kind === "weakened-test" ? "Test" : "Flag"),
        h("span", { class: "flag-msg" }, `Line ${f.line}: ${f.detail || f.title}`)));
    }
    if (callouts.length) el.appendChild(h("div", { class: "callouts" }, callouts));
    return el;
  }

  function fileCounts(file) {
    return h("span", { class: "counts" }, h("span", { class: "add" }, "+" + (file.additions || 0)), " ", h("span", { class: "del" }, "−" + (file.deletions || 0)));
  }

  const STATUS_WORD = { A: "added", D: "deleted", R: "renamed", C: "copied", M: "" };

  function renderFile(file, ctx, hunks) {
    const list = hunks || file.hunks || [];
    const el = h("div", { class: "file", id: (ctx.idPrefix || "") + "file-" + encodeURIComponent(file.path) });
    el.appendChild(h("div", { class: "file-head" },
      h("span", { class: "path" }, file.status === "R" && file.old_path ? `${file.old_path} → ${file.path}` : file.path),
      STATUS_WORD[file.status] ? h("span", { class: "status" }, STATUS_WORD[file.status]) : null,
      file.noise ? h("span", { class: "chip muted" }, file.noise) : null,
      fileCounts(file)));
    if (file.binary) {
      el.appendChild(h("section", { class: ["hunk", "state-" + ((file.file_unit && file.file_unit.state) || "weak")] },
        h("div", { class: "hunk-head" }, "Binary file changed; no text diff.")));
      return el;
    }
    if (!list.length) {
      el.appendChild(h("section", { class: ["hunk", "state-" + ((file.file_unit && file.file_unit.state) || "covered")] },
        h("div", { class: "hunk-head" }, file.status === "R" ? "Renamed without content changes." : "Mode change only.")));
      return el;
    }
    for (const hunk of list) el.appendChild(renderHunk(file, hunk, ctx));
    return el;
  }

  function renderFiles(files, ctx) {
    const frag = document.createDocumentFragment();
    const noise = files.filter((f) => f.noise);
    for (const f of files) if (!f.noise) frag.appendChild(renderFile(f, ctx));
    if (noise.length) {
      const holder = h("div");
      const row = h("div", { class: "noise-row" }, `${D.plural(noise.length, "generated, vendored or lock file")} collapsed`,
        h("button", { class: "btn small", type: "button", onclick: () => { row.remove(); for (const f of noise) holder.appendChild(renderFile(f, ctx)); } }, "Show them"));
      frag.appendChild(row);
      frag.appendChild(holder);
    }
    return frag;
  }

  function context(feature, extra) {
    return Object.assign({
      pid: feature.project.project_id,
      fid: feature.feature_id,
      feature,
      testIndex: testStatusIndex(feature),
      cpIndex: criticalPathIndex(feature),
      flagIndex: flagIndex(feature),
    }, extra || {});
  }

  Object.assign(D, { renderHunk, renderFile, renderFiles, diffContext: context, claimSystems, claimTests, testBadge, TEST_LABEL, fileCounts });
})();
