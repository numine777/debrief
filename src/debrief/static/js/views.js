/* Debrief viewer: pages. Each view renders into the main element and returns nothing. */
(function () {
  "use strict";
  const D = window.Debrief;
  const { h, clear, glyph, data, markdown, ago, clock, pct, plural, short } = D;

  const enc = encodeURIComponent;
  const fpath = (pid, fid) => `#/p/${enc(pid)}/f/${enc(fid)}`;

  // --- shared pieces -------------------------------------------------------------------------

  function loading(root, text) {
    clear(root).appendChild(h("p", { class: "loading" }, text || "Loading…"));
  }

  function sectionTitle(text, aside, level) {
    return h("div", { class: "section-title" }, h(level || "h2", null, text), aside ? h("span", { class: "aside" }, aside) : null);
  }

  function statusPill(status) {
    const words = { in_progress: "In progress", ready_for_review: "Ready for review", merged: "Merged", abandoned: "Abandoned" };
    return h("span", { class: `pill status-${status}` }, words[status] || status || "In progress");
  }

  function miniStrip(row) {
    const strip = h("div", { class: "strip mini", role: "img",
      "aria-label": `${row.covered || 0} explained, ${row.incidental || 0} incidental, ${row.weak || 0} weak, ${row.unclaimed || 0} unexplained hunks` });
    const parts = [["covered", row.covered], ["incidental", row.incidental], ["weak", row.weak], ["unclaimed", row.unclaimed]];
    let any = false;
    for (const [state, n] of parts) {
      if (!n) continue;
      any = true;
      const seg = h("span", { class: `cell state-${state}` });
      seg.style.flexGrow = String(n);
      strip.appendChild(seg);
    }
    if (!any) strip.appendChild(h("span", { class: "cell" }));
    return strip;
  }

  // Link inline `code` in agent prose to the code it names.
  function codeLinker(feature) {
    const files = new Set(((feature.evidence && feature.evidence.files) || []).map((f) => f.path));
    const anchors = {};
    for (const s of feature.systems) for (const a of s.anchors) if (a.symbol) anchors[a.symbol] = s.id;
    const systems = new Set(feature.systems.map((s) => s.id));
    return (root) => {
      for (const code of root.querySelectorAll("code")) {
        if (code.closest("pre") || code.closest("a")) continue;
        const text = code.textContent.trim();
        let href = null;
        if (systems.has(text)) href = `${fpath(feature.project.project_id, feature.feature_id)}/system/${enc(text)}`;
        else if (files.has(text)) href = `${fpath(feature.project.project_id, feature.feature_id)}/diff?mode=systems&path=${enc(text)}`;
        else if (anchors[text]) href = `${fpath(feature.project.project_id, feature.feature_id)}/system/${enc(anchors[text])}`;
        if (href) {
          const a = h("a", { class: "code-link", href });
          code.replaceWith(a);
          a.appendChild(code);
        }
      }
    };
  }

  // Commit bodies are hard-wrapped at about 72 columns; reflow paragraphs so they
  // read as prose, but keep lists and indented blocks line by line.
  function reflow(text) {
    return text.split(/\n\s*\n/).map((para) => {
      const lines = para.split("\n");
      const structured = lines.some((l) => /^\s*([-*+•]|\d+[.)])\s/.test(l) || /^\s{2,}\S/.test(l));
      return { text: structured ? para : lines.map((l) => l.trim()).join(" "), structured };
    });
  }

  function messageBody(body) {
    if (!body) return null;
    const parts = D.splitTrailers(body);
    return [parts.text ? h("div", { class: "card-body" }, reflow(parts.text).map((p) => h("p", { class: p.structured ? "structured" : null }, p.text))) : null,
      parts.trailers.length ? h("div", { class: "trailers" }, parts.trailers.map((line) => h("div", null, line))) : null];
  }

  // --- index --------------------------------------------------------------------------------------

  async function viewIndex(root) {
    loading(root);
    const idx = await data.index();
    const head = h("div", { class: "index-head" },
      h("div", null, h("h1", null, "Features"),
        h("p", null, idx.projects.length ? `Agent work across ${plural(idx.projects.length, "project")}, most recent first.` : "")),
      gotoCommitForm());
    clear(root).appendChild(head);
    if (!idx.projects.length) {
      root.appendChild(h("div", { class: "empty" }, h("h2", null, "Nothing to review yet"),
        h("p", null, "Track a repository with ", h("code", null, "debrief init <repo>"), ", install the agent instructions with ",
          h("code", null, "debrief install"), ", and the next agent session on that repo will show up here.")));
      return;
    }
    for (const project of idx.projects) {
      const section = h("section", { class: "project" },
        h("div", { class: "project-head" }, h("h2", null, project.display_name),
          project.remote_url ? h("span", { class: "remote" }, project.remote_url) : null,
          project.on_this_host ? null : h("span", { class: "muted small" }, "repository not on this host")));
      if (!project.features.length) {
        section.appendChild(h("p", { class: "muted" }, "No features yet. Agents create one when they start a session on a branch."));
      }
      const groups = new Map();
      for (const f of project.features) {
        const key = f.epic || "";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(f);
      }
      const keys = Array.from(groups.keys()).sort((a, b) => (a === "" ? 1 : b === "" ? -1 : a.localeCompare(b)));
      for (const key of keys) {
        if (key) section.appendChild(h("div", { class: "epic-head" }, "Epic", h("a", { href: `#/epic/${enc(key)}` }, key)));
        else if (keys.length > 1) section.appendChild(h("div", { class: "epic-head" }, "Not in an epic"));
        const list = h("ul", { class: "feature-rows" });
        for (const f of groups.get(key)) list.appendChild(featureRow(f));
        section.appendChild(list);
      }
      root.appendChild(section);
    }
  }

  function featureRow(f) {
    const href = fpath(f.project_id, f.feature_id);
    return h("li", { class: "feature-row" },
      h("div", null, h("a", { class: "title", href }, f.title), h("div", { class: "sub" }, f.branch || f.feature_id)),
      h("div", { class: "summary opt" }, (f.summary || "").replace(/\s+/g, " ")),
      h("div", { class: "opt" }, statusPill(f.status)),
      h("div", null, miniStrip(f), h("div", { class: "ratio" },
        f.units ? `${pct(f.coverage)} explained` : "no evidence yet",
        f.open_leg ? h("span", { class: "muted" }, ` · ${f.open_leg} open`) : null)),
      h("div", { class: "opt small" }, f.high ? h("span", { class: "pill sev-high" }, `${f.high} high`) : f.queue ? h("span", { class: "muted" }, `${f.queue} to review`) : h("span", { class: "muted" }, "queue clear")),
      h("div", { class: "when" }, ago(f.updated_at)));
  }

  function gotoCommitForm() {
    const input = h("input", { type: "text", placeholder: "Commit SHA", "aria-label": "Go to a commit by SHA", spellcheck: "false", autocomplete: "off" });
    const form = h("form", { class: "gotosha", onsubmit: (evt) => {
      evt.preventDefault();
      const sha = input.value.trim();
      if (/^[0-9a-fA-F]{4,64}$/.test(sha)) location.hash = `#/commit/${sha.toLowerCase()}`;
      else D.toast("Enter at least four hex characters of a SHA");
    } }, input, h("button", { class: "btn", type: "submit" }, "Go to commit"));
    return form;
  }

  // --- feature shell ------------------------------------------------------------------------------

  const TABS = [
    ["brief", "Brief"], ["map", "System map"], ["systems", "Systems"], ["tests", "Tests"],
    ["queue", "Review queue"], ["diff", "Diff"], ["timeline", "Timeline"],
  ];

  async function viewFeature(root, route) {
    const { pid, fid } = route;
    loading(root);
    const feature = await data.feature(pid, fid);
    D.setCrumbs([[feature.project.display_name, "#/"], [feature.title, fpath(pid, fid)]]);
    clear(root);
    const tab = route.tab || "brief";
    root.appendChild(featureHeader(feature, tab, route));
    root.appendChild(featureTabs(feature, tab));
    const body = h("div", { class: "feature-body" });
    root.appendChild(body);
    const views = {
      brief: viewBrief, map: viewMap, systems: viewSystems, system: viewSystem, tests: viewTests, queue: viewQueue,
      diff: viewDiff, timeline: viewTimeline, decision: viewDecision,
    };
    const extra = D.extraFeatureViews || {};
    const fn = extra[tab] || views[tab];
    if (!fn) {
      body.appendChild(D.errorBox({ status: 404, message: `There is no ${tab} view.` }));
      return;
    }
    await fn(body, feature, route);
  }

  // The views of a feature. On a phone the bar sticks to the top of the screen and scrolls
  // sideways: the current view is brought into sight and a fade marks the side that hides more.
  function featureTabs(feature, tab) {
    const ev = feature.evidence || {};
    const base = fpath(feature.project.project_id, feature.feature_id);
    const queue = ev.queue || [];
    const high = queue.filter((i) => i.severity === "high").length;
    const counts = { queue: queue.length ? h("span", { class: ["count", high && "alert"] }, String(queue.length)) : null,
      systems: h("span", { class: "count" }, String(feature.systems.length)),
      tests: h("span", { class: "count" }, String(feature.tests.tests.length)) };
    Object.assign(counts, D.extraTabCounts ? D.extraTabCounts(feature) : {});
    const nav = h("nav", { class: "tabs", "aria-label": "Feature views", onscroll: () => tabEdges(bar) },
      TABS.concat(D.extraTabs || []).map(([id, label]) =>
        h("a", { href: id === "brief" ? base : `${base}/${id}`, "aria-current": (tab === id || (id === "systems" && tab === "system")) ? "page" : null },
          label, counts[id] || null)));
    const bar = h("div", { class: "tabs-bar" }, nav);
    requestAnimationFrame(() => {
      const current = nav.querySelector('[aria-current="page"]');
      if (current && nav.scrollWidth > nav.clientWidth) {
        const box = nav.getBoundingClientRect(), cur = current.getBoundingClientRect();
        nav.scrollLeft += cur.left - box.left - (box.width - cur.width) / 2;
      }
      tabEdges(bar);
    });
    return bar;
  }

  function tabEdges(bar) {
    const nav = bar.firstChild;
    bar.classList.toggle("more-left", nav.scrollLeft > 2);
    bar.classList.toggle("more-right", nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 2);
  }
  window.addEventListener("resize", () => { const bar = document.querySelector(".tabs-bar"); if (bar) tabEdges(bar); });

  function featureHeader(feature, tab, route) {
    const ev = feature.evidence || {};
    const cov = ev.coverage || {};
    const base = fpath(feature.project.project_id, feature.feature_id);
    const legs = h("span", { class: "legs", "aria-label": "Legs" }, feature.legs.map((leg) => h("a", {
      class: ["leg", !leg.closed_at && "open"], href: `${base}/timeline#leg-${leg.leg_id}`,
      title: leg.closed_at ? `Closed ${clock(leg.closed_at)}` : "Open: the developer closes it" },
      leg.leg_id.replace("leg-", "Leg "), leg.closed_at ? "" : " open")));
    const refresh = D.exported ? null : h("button", { class: "btn small", type: "button", title: "Recompute evidence from git now",
      onclick: async (evt) => {
        evt.currentTarget.disabled = true;
        try {
          const res = await data.post(`${data.featurePath(feature.project.project_id, feature.feature_id)}/ingest`);
          D.invalidate();
          D.toast(res.summary);
          D.rerender();
        } catch (err) { D.toast(err.message); evt.currentTarget.disabled = false; }
      } }, D.icon("refresh"), "Refresh");
    const landed = (feature.landed || [])[0];
    const pickHunk = (u) => { location.hash = `${base}/diff?mode=systems&hunk=${u.id}`; };
    const unexplained = cov.unclaimed || 0;
    // Off the Brief, a phone shows a compact header: title, strip, the total and what needs review.
    return h("header", { class: ["feature-head", tab !== "brief" && "compact"] },
      h("h1", null, feature.title),
      h("div", { class: "facts" },
        feature.branch ? h("span", { class: "branch" }, feature.branch) : null,
        statusPill(feature.status),
        feature.legs.length ? legs : null,
        feature.epic ? h("a", { class: "chip", href: `#/epic/${enc(feature.epic)}` }, `Epic ${feature.epic}`) : null,
        landed ? h("a", { class: "chip", href: `#/commit/${landed.sha}` }, `Landed as ${short(landed.sha)}`) : null,
        h("span", { class: "when" }, ev.computed_at ? `Evidence from ${ago(ev.computed_at)}` : "No evidence yet",
          ev.computed_on && D.meta && D.meta.host && ev.computed_on !== D.meta.host ? `, computed on ${ev.computed_on}` : "",
          ev.repo_available === false ? " (repository not on this host)" : ""),
        refresh),
      h("div", null,
        D.coverageStrip(ev.files || [], { onPick: pickHunk, reviewed: D.reviewedSet ? D.reviewedSet(feature) : null,
          label: `Coverage: ${pct(cov.ratio)} of ${cov.units || 0} changed hunks explained` }),
        h("div", { class: "legend" },
          h("span", { class: "legend-total" }, h("b", null, pct(cov.ratio)), `explained of ${plural(cov.units || 0, "changed hunk")}`),
          Array.from(D.coverageLegend(cov).childNodes),
          D.headerExtras ? D.headerExtras(feature) : null,
          unexplained ? h("a", { class: "legend-alert", href: `${base}/diff?mode=systems#unexplained` }, `Review ${plural(unexplained, "unexplained hunk")}`) : null)));
  }

  // --- brief --------------------------------------------------------------------------------------------

  function targetHref(feature, target) {
    const base = fpath(feature.project.project_id, feature.feature_id);
    if (target.startsWith("tests/")) return `${base}/tests`;
    const [sid, cp] = target.split("/");
    if (feature.systems.some((s) => s.id === sid)) return `${base}/system/${enc(sid)}${cp ? "?cp=" + enc(cp) : ""}`;
    if (feature.decisions.some((d) => d.id === target)) return `${base}/decision/${enc(target)}`;
    return null;
  }

  async function viewBrief(root, feature) {
    const ev = feature.evidence || {};
    const linker = codeLinker(feature);
    const main = h("div");
    if (!feature.brief.exists) {
      main.appendChild(h("div", { class: "empty" }, h("h2", null, "No brief yet"),
        h("p", null, "The agent writes the brief when the developer asks it to close out a leg. Until then, the ",
          h("a", { href: `${fpath(feature.project.project_id, feature.feature_id)}/timeline` }, "timeline"), " and the ",
          h("a", { href: `${fpath(feature.project.project_id, feature.feature_id)}/diff` }, "diff"), " show the work in progress.")));
    } else {
      for (const section of feature.brief.sections) {
        const block = h("section", { class: "brief-section" });
        if (section.title) block.appendChild(h("h2", null, section.title));
        block.appendChild(markdown(section.markdown, { linkCode: linker }));
        main.appendChild(block);
      }
    }
    const aside = h("aside");
    const targets = (feature.brief.meta.review_first || []);
    if (targets.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Review these first"),
        targets.map((t) => {
          const href = targetHref(feature, t.target);
          return h(href ? "a" : "div", { class: "target", href },
            h("div", { class: "target-name" }, t.target), t.why ? h("div", { class: "target-why" }, t.why) : null);
        })));
    }
    const stats = ev.stats || {};
    const tests = ev.tests || [];
    const verified = tests.filter((t) => t.status === "verified_pass").length;
    const queue = ev.queue || [];
    const sev = (s) => queue.filter((i) => i.severity === s).length;
    aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Evidence"),
      h("dl", { class: "stat-list" },
        h("dt", null, "Explained"), h("dd", null, `${pct((ev.coverage || {}).ratio)} of ${plural((ev.coverage || {}).units || 0, "hunk")}`),
        h("dt", null, "Review queue"), h("dd", null, `${sev("high")} high, ${sev("medium")} medium, ${sev("low")} low`),
        h("dt", null, "Tests"), h("dd", null, `${verified} of ${tests.length} verified by a recorded run`),
        h("dt", null, "Commits"), h("dd", null, `${stats.agent_commits || 0} agent, ${stats.developer_commits || 0} developer`),
        h("dt", null, "Change"), h("dd", null, `${stats.files || 0} files, +${stats.additions || 0} −${stats.deletions || 0}`),
        h("dt", null, "Sessions"), h("dd", null, `${stats.sessions || 0} sessions, ${stats.runs || 0} recorded runs`))));
    aside.appendChild(legsBlock(feature));
    if (D.briefExtras) D.briefExtras(aside, feature);
    if (feature.issues.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, `Record issues (${feature.issues.length})`),
        h("ul", { class: "issues-list" }, feature.issues.slice(0, 12).map((i) => h("li", null,
          h("span", { class: "rec" }, i.record), " ", h("span", { class: "lvl-" + i.level }, i.level === "error" ? "Error: " : ""), i.message))),
        feature.issues.length > 12 ? h("a", { href: `${fpath(feature.project.project_id, feature.feature_id)}/queue` }, "All issues in the review queue") : null));
    }
    if (feature.squash && feature.squash.record) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Consolidated record"),
        h("p", { class: "small" }, "This feature was squashed into one record that links every leg and commit."),
        h("details", null, h("summary", null, "Read it"), markdown(feature.squash.record.replace(/^---[\s\S]*?---\n/, "")))));
    }
    root.appendChild(h("div", { class: "two-col" }, main, aside));
  }

  function legsBlock(feature) {
    const block = h("div", { class: "aside-block" }, h("h3", null, "Legs"));
    if (!feature.legs.length) {
      block.appendChild(h("p", { class: "small muted" }, "No sessions yet."));
      return block;
    }
    const list = h("ul", { class: "leg-list" });
    for (const leg of feature.legs) {
      const ev = leg.evidence || {};
      const commits = (ev.commits || []).length;
      list.appendChild(h("li", null, h("span", { class: "leg-id" }, leg.leg_id),
        h("span", null, leg.closed_at ? `Closed ${ago(leg.closed_at)}` : h("strong", null, "Open"),
          `, ${plural(commits, "commit")}, ${plural((leg.sessions || []).length, "session")}`,
          leg.close_requested_at && !leg.closed_at ? h("div", { class: "small muted" }, `Close requested ${ago(leg.close_requested_at)}`) : null)));
    }
    block.appendChild(list);
    if (D.legActions) D.legActions(block, feature);
    return block;
  }

  // --- system map -------------------------------------------------------------------------------------------

  function layout(nodes, edges) {
    const ids = nodes.map((n) => n.id);
    const out = new Map(ids.map((id) => [id, []]));
    const inc = new Map(ids.map((id) => [id, []]));
    for (const e of edges) { out.get(e.from).push(e.to); inc.get(e.to).push(e.from); }
    // Longest-path layering from the systems nothing depends on; back edges are ignored.
    const layer = new Map();
    const visiting = new Set();
    function depth(id) {
      if (layer.has(id)) return layer.get(id);
      if (visiting.has(id)) return 0;
      visiting.add(id);
      let d = 0;
      for (const parent of inc.get(id)) d = Math.max(d, depth(parent) + 1);
      visiting.delete(id);
      layer.set(id, d);
      return d;
    }
    ids.forEach(depth);
    const layers = [];
    for (const id of ids) {
      const l = layer.get(id);
      (layers[l] = layers[l] || []).push(id);
    }
    // Two barycenter sweeps to reduce crossings.
    const pos = new Map();
    layers.forEach((row) => row.forEach((id, i) => pos.set(id, i)));
    for (let sweep = 0; sweep < 4; sweep++) {
      const down = sweep % 2 === 0;
      const order = down ? layers.slice(1) : layers.slice(0, -1).reverse();
      for (const row of order) {
        const score = (id) => {
          const near = down ? inc.get(id) : out.get(id);
          if (!near.length) return pos.get(id);
          return near.reduce((sum, n) => sum + pos.get(n), 0) / near.length;
        };
        row.sort((a, b) => score(a) - score(b));
        row.forEach((id, i) => pos.set(id, i));
      }
    }
    return layers;
  }

  function systemMap(systems, opts) {
    opts = opts || {};
    const nodes = systems.map((s) => ({ id: s.id, title: s.title || s.id, change: s.change, data: s }));
    const known = new Set(nodes.map((n) => n.id));
    const edges = [];
    for (const s of systems) for (const dep of s.depends_on || []) if (known.has(dep.system) && dep.system !== s.id) edges.push({ from: s.id, to: dep.system, label: dep.relation || "" });
    const layers = layout(nodes, edges);
    const W = 228, H = 84, GX = 44, GY = 92, PAD = 24;
    const widest = Math.max(1, ...layers.map((r) => r.length));
    const width = PAD * 2 + widest * W + (widest - 1) * GX;
    const height = PAD * 2 + layers.length * H + Math.max(0, layers.length - 1) * GY;
    const at = new Map();
    layers.forEach((row, li) => {
      const rowWidth = row.length * W + (row.length - 1) * GX;
      const x0 = (width - rowWidth) / 2;
      row.forEach((id, i) => at.set(id, { x: x0 + i * (W + GX), y: PAD + li * (H + GY) }));
    });
    const svg = h("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img",
      class: edges.length <= 8 ? "labels-all" : null,
      "aria-label": `System map: ${nodes.length} systems, ${edges.length} dependencies` });
    const edgeGroup = h("g");
    const nodeGroup = h("g");
    svg.appendChild(edgeGroup);
    svg.appendChild(nodeGroup);
    const edgeEls = [];
    for (const e of edges) {
      const a = at.get(e.from), b = at.get(e.to);
      let x1 = a.x + W / 2, y1 = a.y + H, x2 = b.x + W / 2, y2 = b.y;
      let d;
      if (b.y > a.y) {
        const my = (y1 + y2) / 2;
        d = `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2 - 6}`;
      } else {
        // An upward or sideways edge (a cycle): route around the side.
        x1 = a.x + W; y1 = a.y + H / 2; x2 = b.x + W; y2 = b.y + H / 2;
        d = `M${x1},${y1} C${x1 + 60},${y1} ${x2 + 60},${y2} ${x2 + 6},${y2}`;
      }
      const label = e.label.length > 34 ? e.label.slice(0, 32) + "…" : e.label;
      const g = h("g", { class: "map-edge", dataset: { from: e.from, to: e.to } },
        h("title", null, `${e.from} depends on ${e.to}${e.label ? ": " + e.label : ""}`),
        h("path", { d, "marker-end": "url(#arrow)" }),
        label && edges.length <= 40 ? h("text", { class: "label", x: (x1 + x2) / 2 + 6, y: (y1 + y2) / 2, "paint-order": "stroke",
          stroke: "var(--sheet)", "stroke-width": "4" }, label) : null);
      edgeGroup.appendChild(g);
      edgeEls.push(g);
    }
    svg.insertBefore(h("defs", null, h("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "8", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
      h("path", { d: "M0,0 L10,5 L0,10 z", fill: "var(--rule-strong)" }))), svg.firstChild);
    for (const n of nodes) {
      const p = at.get(n.id);
      const s = n.data;
      const title = n.title.length > 30 ? n.title.slice(0, 29) + "…" : n.title;
      const cps = (s.critical_paths || []).length;
      // Short lines that fit the node (about 30 characters of 11px mono): the change and size, then critical paths.
      const meta = [n.change || "", s.hunk_count !== undefined ? plural(s.hunk_count, "hunk") : null].filter(Boolean).join(", ");
      const cpText = cps ? plural(cps, "critical path") : "";
      const fit = (text) => (text.length > 30 ? text.slice(0, 29) + "…" : text);
      const g = h("g", { class: ["map-node", "change-" + (n.change || "touched")], tabindex: "0", role: "link",
        "aria-label": `${n.title}, ${n.change || "touched"}`, transform: `translate(${p.x},${p.y})` },
        h("title", null, `${n.title} (${n.id})`),
        h("rect", { class: "frame", width: W, height: H, rx: 2 }),
        h("text", { class: "title", x: 12, y: 26 }, title),
        h("text", { class: "meta", x: 12, y: 44 }, fit(n.id)),
        h("text", { class: "meta", x: 12, y: 59 }, fit(meta)),
        cpText ? h("text", { class: "meta", x: 12, y: 74 }, fit(cpText)) : null);
      const go = () => { if (opts.onPick) opts.onPick(n.id); };
      g.addEventListener("click", go);
      g.addEventListener("keydown", (evt) => { if (evt.key === "Enter") go(); });
      const hot = (on) => {
        svg.classList.toggle("focused", on);
        edgeEls.forEach((el) => {
          if (el.dataset.from === n.id || el.dataset.to === n.id) el.classList.toggle("hot", on);
        });
      };
      g.addEventListener("mouseenter", () => hot(true));
      g.addEventListener("mouseleave", () => hot(false));
      g.addEventListener("focus", () => hot(true));
      g.addEventListener("blur", () => hot(false));
      nodeGroup.appendChild(g);
    }
    return svg;
  }

  function mapLegend() {
    const frame = (cls) => h("svg", { width: 30, height: 16, viewBox: "0 0 30 16", "aria-hidden": "true" },
      h("rect", { x: 1, y: 1, width: 28, height: 14, rx: 1.5, fill: "var(--sheet)", stroke: cls === "new" ? "var(--ink)" : cls === "touched" ? "var(--ink-3)" : "var(--ink-2)",
        "stroke-width": cls === "new" ? 2.6 : 1.5, "stroke-dasharray": cls === "touched" ? "4 3" : null }));
    return h("div", { class: "map-legend" },
      h("span", null, frame("new"), " New system"), h("span", null, frame("modified"), " Modified"), h("span", null, frame("touched"), " Touched"),
      h("span", null, "Arrows point from a system to what it depends on."));
  }

  async function viewMap(root, feature) {
    if (!feature.systems.length) {
      root.appendChild(h("div", { class: "empty" }, h("h2", null, "No systems yet"),
        h("p", null, "Systems are written at closeout. Each one explains a mechanism and anchors it to code.")));
      return;
    }
    const base = fpath(feature.project.project_id, feature.feature_id);
    root.appendChild(mapLegend());
    root.appendChild(h("div", { class: "map-wrap" }, systemMap(feature.systems, { onPick: (sid) => { location.hash = `${base}/system/${enc(sid)}`; } })));
  }

  // --- systems ----------------------------------------------------------------------------------------

  function testSummary(statuses) {
    if (!statuses.length) return h("span", { class: "muted" }, "none");
    const ok = statuses.filter((s) => s === "verified_pass").length;
    const fail = statuses.filter((s) => s === "verified_fail").length;
    return h("span", null, `${ok} of ${statuses.length} verified`, fail ? h("span", { class: "ts-verified_fail" }, `, ${fail} failing`) : null);
  }

  // Tables that turn into one card per row on a phone. Each cell carries its column's name so the
  // card can label it, and explicit roles keep the table semantics once CSS stops laying out a table.
  function cardTable(cls, headings, rows) {
    const table = h("table", { class: ["grid", "cards", cls], role: "table" },
      h("thead", { role: "rowgroup" }, h("tr", { role: "row" }, headings.map((t) => h("th", { role: "columnheader" }, t)))),
      h("tbody", { role: "rowgroup" }, rows));
    for (const tr of table.tBodies[0].rows) {
      tr.setAttribute("role", "row");
      for (const td of tr.cells) td.setAttribute("role", "cell");
    }
    return table;
  }

  async function viewSystems(root, feature) {
    if (!feature.systems.length) return viewMap(root, feature);
    const base = fpath(feature.project.project_id, feature.feature_id);
    const rows = feature.systems.map((s) => {
      const untested = s.critical_paths.filter((cp) => cp.status === "untested").length;
      return h("tr", null,
        h("td", { class: "lead" }, h("a", { href: `${base}/system/${enc(s.id)}` }, h("strong", null, s.title)), h("div", { class: "mono muted small" }, s.id)),
        h("td", { "data-label": "Change" }, s.change || ""),
        h("td", { class: "num", "data-label": "Files" }, String(s.files.length)),
        h("td", { class: "num", "data-label": "Lines" }, String(s.lines_changed)),
        h("td", { "data-label": "Critical paths" }, s.critical_paths.length ? `${s.critical_paths.length}${untested ? `, ${untested} untested` : ""}` : h("span", { class: "muted" }, "none")),
        h("td", { "data-label": "Tests" }, testSummary(s.test_statuses)),
        h("td", { class: "num", "data-label": "Issues" }, s.issues.length ? h("span", { class: "ts-claimed_only" }, String(s.issues.length)) : ""));
    });
    root.appendChild(cardTable("systems-table", ["System", "Change", "Files", "Lines", "Critical paths", "Tests", "Issues"], rows));
  }

  async function viewSystem(root, feature, route) {
    const sid = route.sub;
    const system = feature.systems.find((s) => s.id === sid);
    const base = fpath(feature.project.project_id, feature.feature_id);
    if (!system) {
      root.appendChild(D.errorBox({ status: 404, message: `No system ${sid} in this feature.` }));
      return;
    }
    const usedBy = feature.systems.filter((s) => (s.depends_on || []).some((d) => d.system === sid));
    const linker = codeLinker(feature);
    const main = h("div", null,
      h("p", null, h("a", { href: `${base}/systems` }, "All systems")),
      h("h2", { class: "system-title" }, system.title),
      h("div", { class: "facts chips" }, h("span", { class: "chip mono" }, system.id), h("span", { class: "chip" }, system.change || "touched")),
      system.sections.map((sec) => h("section", { class: "brief-section" }, sec.title ? h("h2", null, sec.title) : null,
        markdown(sec.markdown, { linkCode: linker }))));
    const codeHolder = h("div", null, h("p", { class: "loading" }, "Loading the code this system explains…"));
    main.appendChild(sectionTitle("Code this system explains", `${plural(system.hunk_count, "hunk")} in ${plural(system.files.length, "file")}`));
    main.appendChild(codeHolder);

    const aside = h("aside");
    if (system.critical_paths.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Critical paths"),
        system.critical_paths.map((cp) => h("div", { class: "cp", id: "cp-" + cp.id },
          h("div", { class: "cp-head" }, h("span", { class: "cp-id" }, cp.id), h("span", { class: "cp-kind" }, cp.kind || ""),
            cp.status === "tested" ? D.testBadge(cp.verified ? "verified_pass" : "claimed_only") : h("span", { class: "small " + (cp.status === "gap" ? "muted" : "ts-claimed_only") }, cp.status === "gap" ? "known gap" : "no test")),
          h("div", { class: "cp-inv" }, cp.invariant || "No invariant given."),
          cp.failure_mode ? h("div", { class: "cp-fail" }, "If it fails: ", cp.failure_mode) : null,
          cp.anchor ? h("div", { class: "mono small muted" }, `${cp.anchor.path}${cp.anchor.symbol ? " · " + cp.anchor.symbol : ""}`) : null))));
    }
    const deps = (system.depends_on || []);
    if (deps.length || usedBy.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Connections"),
        deps.length ? h("div", null, h("div", { class: "small muted" }, "Depends on"), h("ul", { class: "anchor-list" }, deps.map((d) =>
          h("li", null, h("a", { href: `${base}/system/${enc(d.system)}` }, d.system), d.relation ? h("span", { class: "muted" }, d.relation) : null)))) : null,
        usedBy.length ? h("div", null, h("div", { class: "small muted" }, "Used by"), h("ul", { class: "anchor-list" }, usedBy.map((s) =>
          h("li", null, h("a", { href: `${base}/system/${enc(s.id)}` }, s.id),
            h("span", { class: "muted" }, ((s.depends_on || []).find((d) => d.system === sid) || {}).relation || ""))))) : null));
    }
    const testIdx = {};
    for (const t of (feature.evidence.tests || [])) testIdx[t.id] = t;
    const tests = feature.tests.tests.filter((t) => system.tests.includes(t.id));
    if (tests.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Tests"),
        h("ul", { class: "anchor-list" }, tests.map((t) => h("li", null, testIdx[t.id] ? D.testBadge(testIdx[t.id].status) : null,
          h("a", { href: `${base}/tests`, class: "mono" }, t.id))))));
    }
    if (system.decisions.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Decisions"),
        h("ul", { class: "anchor-list" }, system.decisions.map((did) => {
          const d = feature.decisions.find((x) => x.id === did);
          return h("li", null, h("a", { href: `${base}/decision/${enc(did)}` }, d ? d.title : did));
        }))));
    }
    const anchors = (feature.evidence.anchors || []).filter((a) => a.owner === sid && a.owner_kind === "system");
    if (anchors.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Anchors"),
        h("ul", { class: "anchor-list" }, anchors.map((a) => h("li", { class: a.status === "stale" ? "anchor-stale" : null },
          glyph(a.status === "stale" ? "unclaimed" : a.status === "path" ? "weak" : "covered"),
          h("span", { class: "mono" }, `${a.path}${a.symbol ? ":" + a.symbol : ""}${a.range ? ` (${a.range[0]}–${a.range[1]})` : ""}`),
          a.status === "stale" ? h("span", { class: "small" }, a.reason || "stale") : null)))));
    }
    if (system.issues.length) {
      aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Record issues"),
        h("ul", { class: "issues-list" }, system.issues.map((i) => h("li", null, i.message)))));
    }
    root.appendChild(h("div", { class: "two-col" }, main, aside));
    if (route.query && route.query.cp) {
      const el = document.getElementById("cp-" + route.query.cp);
      if (el) el.scrollIntoView({ block: "center" });
    }
    try {
      const diff = await data.diff(feature.project.project_id, feature.feature_id, "feature");
      clear(codeHolder);
      const ctx = D.diffContext(feature, D.diffHooks ? D.diffHooks(feature) : {});
      let any = false;
      for (const file of diff.files) {
        const hunks = file.hunks.filter((hk) => D.claimSystems(hk).includes(sid));
        if (!hunks.length) continue;
        any = true;
        codeHolder.appendChild(D.renderFile(file, ctx, hunks));
      }
      if (!any) codeHolder.appendChild(h("p", { class: "muted" }, "No changed hunk falls inside this system's anchors."));
    } catch (err) {
      clear(codeHolder).appendChild(D.errorBox(err));
    }
  }

  // --- decisions ------------------------------------------------------------------------------------------

  async function viewDecision(root, feature, route) {
    const d = feature.decisions.find((x) => x.id === route.sub);
    const base = fpath(feature.project.project_id, feature.feature_id);
    if (!d) {
      root.appendChild(D.errorBox({ status: 404, message: `No decision ${route.sub}.` }));
      return;
    }
    root.appendChild(h("div", { class: "two-col" },
      h("div", null, h("h2", null, d.title || d.id),
        d.sections.map((sec) => h("section", { class: "brief-section" }, sec.title ? h("h2", null, sec.title) : null, markdown(sec.markdown, { linkCode: codeLinker(feature) })))),
      h("aside", null, h("div", { class: "aside-block" }, h("h3", null, "Decision"),
        h("dl", { class: "stat-list" }, h("dt", null, "Id"), h("dd", { class: "mono small" }, d.id),
          h("dt", null, "Status"), h("dd", null, d.status || "accepted"),
          h("dt", null, "Reversibility"), h("dd", null, d.reversibility || "unknown")),
        d.systems.length ? h("div", { class: "chips" }, d.systems.map((sid) => h("a", { class: "chip system", href: `${base}/system/${enc(sid)}` }, sid))) : null))));
  }

  // --- tests ----------------------------------------------------------------------------------------------

  async function viewTests(root, feature) {
    const base = fpath(feature.project.project_id, feature.feature_id);
    const evTests = {};
    for (const t of feature.evidence.tests || []) evTests[t.id] = t;
    if (!feature.tests.exists) {
      root.appendChild(h("div", { class: "empty" }, h("h2", null, "No test claims yet"),
        h("p", null, "The agent lists test groups, what each proves and how to run it, in tests.yaml at closeout.")));
    } else {
      const verified = feature.tests.tests.filter((t) => (evTests[t.id] || {}).status === "verified_pass").length;
      root.appendChild(sectionTitle("Test claims", `${verified} of ${feature.tests.tests.length} verified by a recorded run`));
      const targetLink = (v) => {
        const [sid] = v.split("/");
        return h("a", { class: "chip system", href: `${base}/system/${enc(sid)}${v.includes("/") ? "?cp=" + enc(v.split("/")[1]) : ""}` }, v);
      };
      root.appendChild(cardTable("tests-table", ["Evidence", "Test", "What it proves", "Validates", "Command", "Last run"],
        feature.tests.tests.map((t) => {
          const ev = evTests[t.id] || {};
          const last = (ev.runs || [])[ev.runs ? ev.runs.length - 1 : 0];
          return h("tr", null,
            h("td", null, D.testBadge(ev.status || "not_run"), t.claimed_result && ev.status !== "verified_" + (t.claimed_result === "pass" ? "pass" : "fail")
              ? h("div", { class: "small muted" }, `claimed ${String(t.claimed_result).replace("_", " ")}`) : null),
            h("td", null, h("span", { class: "mono small tid" }, t.id), h("div", { class: "small muted" }, t.kind || "")),
            h("td", { class: "claim" }, t.claim),
            h("td", { class: "validates", "data-label": "Validates" }, h("div", { class: "chips" }, t.validates.map(targetLink))),
            h("td", { class: "command", "data-label": "Command" }, h("code", null, t.command || "")),
            h("td", { class: "small", "data-label": "Last run" }, last ? h("span", null, last.exit_code === 0 ? "passed" : `exit ${last.exit_code}`, h("div", { class: "muted" }, ago(last.started_at)),
              ev.current ? null : h("div", { class: "muted" }, "at an older commit")) : h("span", { class: "muted" }, "none recorded")));
        })));
      const gaps = feature.tests.gaps || [];
      root.appendChild(sectionTitle("Known gaps", gaps.length ? plural(gaps.length, "gap") : "the agent listed none"));
      if (gaps.length) {
        root.appendChild(h("ul", { class: "issues-list" }, gaps.map((g) => h("li", null, g.validates ? h("span", { class: "mono small" }, g.validates + "  ") : null,
          h("span", { class: "claim" }, g.note)))));
      }
    }
    const untested = (feature.evidence.critical_paths || []).filter((cp) => cp.status === "untested");
    root.appendChild(sectionTitle("Critical paths with no test", untested.length ? plural(untested.length, "path") : "none"));
    if (untested.length) {
      root.appendChild(h("ul", { class: "issues-list" }, untested.map((cp) => h("li", null,
        h("a", { class: "mono small", href: `${base}/system/${enc(cp.system)}?cp=${enc(cp.id)}` }, cp.ref), "  ", cp.invariant))));
    }
  }

  // --- review queue -------------------------------------------------------------------------------------

  const KIND_LABEL = {
    "undeclared-critical": "Undeclared critical path", "unclaimed-hunk": "Unexplained change", "weak-claim": "Weak claim",
    "stale-anchor": "Stale anchor", "weakened-test": "Weakened test", risky: "Risky edit", "claim-only-test": "Claim-only test",
    "failing-test": "Failing test", "untested-critical-path": "Untested critical path", "non-atomic-commit": "Non-atomic commit",
    "thin-commit-message": "Thin commit message", "record-issue": "Record issue", "sync-conflict": "Sync conflict",
  };

  async function viewQueue(root, feature, route) {
    const base = fpath(feature.project.project_id, feature.feature_id);
    const queue = feature.evidence.queue || [];
    if (!queue.length) {
      root.appendChild(h("div", { class: "empty" }, h("h2", null, "The review queue is empty"),
        h("p", null, feature.evidence.computed_at ? "Every changed hunk is explained and nothing was flagged." : "Evidence hasn't been computed yet. Use Refresh.")));
      return;
    }
    const kinds = {};
    for (const item of queue) kinds[item.kind] = (kinds[item.kind] || 0) + 1;
    let active = (route.query && route.query.kind) || null;
    const list = h("div");
    const filters = h("div", { class: "filters", role: "group", "aria-label": "Filter by kind" });
    const drawFilters = () => {
      clear(filters);
      filters.appendChild(h("button", { type: "button", "aria-pressed": String(!active), onclick: () => { active = null; draw(); } }, `All ${queue.length}`));
      for (const [kind, n] of Object.entries(kinds)) {
        filters.appendChild(h("button", { type: "button", "aria-pressed": String(active === kind), onclick: () => { active = kind; draw(); } },
          `${KIND_LABEL[kind] || kind} ${n}`));
      }
    };
    const itemLinks = (item) => {
      const links = [];
      if (item.hunk_id) links.push(h("a", { class: "btn small", href: `${base}/diff?mode=systems&hunk=${item.hunk_id}` }, "Open code"));
      if (item.commit) links.push(h("a", { class: "btn small", href: `#/commit/${item.commit}` }, "Open commit"));
      if (item.near && item.near.system) links.push(h("a", { class: "btn small", href: `${base}/system/${enc(item.near.system)}${item.near.critical_path ? "?cp=" + enc(item.near.critical_path) : ""}` },
        `Nearest: ${item.near.system}`));
      if (item.test) links.push(h("a", { class: "btn small", href: `${base}/tests` }, "Tests"));
      if (item.record && /^systems\/(.+)\.md$/.test(item.record)) {
        links.push(h("a", { class: "btn small", href: `${base}/system/${enc(item.record.match(/^systems\/(.+)\.md$/)[1])}` }, "Open system"));
      }
      return links;
    };
    const draw = () => {
      drawFilters();
      clear(list);
      for (const sev of ["high", "medium", "low"]) {
        const items = queue.filter((i) => i.severity === sev && (!active || i.kind === active));
        if (!items.length) continue;
        list.appendChild(sectionTitle(sev === "high" ? "High" : sev === "medium" ? "Medium" : "Low", plural(items.length, "item"), "h3"));
        list.appendChild(h("ul", { class: "queue-list" }, items.map((item) => h("li", { class: ["queue-item", item.undeclared && "undeclared"] },
          h("div", null, h("div", { class: "kind" }, KIND_LABEL[item.kind] || item.kind, item.path ? h("span", { class: "mono" }, `  ${item.path}${item.line ? ":" + item.line : ""}`) : null),
            h("div", { class: "title" }, item.title), item.detail ? h("div", { class: ["detail", item.rule && item.kind !== "undeclared-critical" && "code"] }, item.detail) : null),
          h("div", { class: "links" }, itemLinks(item))))));
      }
    };
    root.appendChild(h("p", { class: "muted small" }, "Sorted by severity. Within a severity, problems the agent didn't declare come first (marked with a diamond)."));
    root.appendChild(filters);
    root.appendChild(list);
    draw();
  }

  // --- diff: Story and Systems modes -------------------------------------------------------------------

  async function viewDiff(root, feature, route) {
    const q = route.query || {};
    const mode = q.mode === "systems" ? "systems" : (q.hunk || q.path ? "systems" : (D.prefs().diffMode || "story"));
    const base = fpath(feature.project.project_id, feature.feature_id);
    const ev = feature.evidence || {};
    const cov = ev.coverage || {};
    const setMode = (m) => { D.savePref("diffMode", m); location.hash = `${base}/diff?mode=${m}`; };
    const toolbar = h("div", { class: "diff-toolbar" },
      h("div", { class: "segmented", role: "group", "aria-label": "Diff mode" },
        h("button", { type: "button", "aria-pressed": String(mode === "story"), onclick: () => setMode("story") }, "Story"),
        h("button", { type: "button", "aria-pressed": String(mode === "systems"), onclick: () => setMode("systems") }, "Systems")),
      h("span", { class: "small muted" }, mode === "story" ? "Commits in order, each led by its message." : "The same changes grouped under each system's Change section."));
    const completeness = h("span", { class: "completeness" },
      cov.unclaimed ? [glyph("unclaimed"), `${plural(cov.unclaimed, "hunk")} unexplained`] : cov.units ? [glyph("covered"), "Every hunk is explained"] : "No changes yet",
      cov.weak ? h("span", { class: "muted" }, ` · ${cov.weak} weak`) : null);
    toolbar.appendChild(completeness);
    root.appendChild(toolbar);
    const outline = h("nav", { class: "outline", "aria-label": "Outline" });
    const stream = h("div", { class: "stream" });
    root.appendChild(h("div", { class: "diff-layout" }, outline, stream));
    if (mode === "story") await storyMode(feature, route, outline, stream, toolbar);
    else await systemsMode(feature, route, outline, stream);
  }

  async function storyMode(feature, route, outline, stream, toolbar) {
    const q = route.query || {};
    const base = fpath(feature.project.project_id, feature.feature_id);
    const commits = (feature.evidence.commits || []).filter((c) => c.kind !== "merge" || (q.merges === "1"));
    const legIds = feature.legs.map((l) => l.leg_id);
    const legFilter = q.leg && (legIds.includes(q.leg) || q.leg === "pending") ? q.leg : "";
    const select = h("select", { "aria-label": "Leg", onchange: (evt) => {
      const v = evt.currentTarget.value;
      location.hash = `${base}/diff?mode=story${v ? "&leg=" + enc(v) : ""}`;
    } }, h("option", { value: "" }, "All legs"), legIds.map((id) => h("option", { value: id, selected: legFilter === id }, id)),
      commits.some((c) => !c.leg_id) ? h("option", { value: "pending", selected: legFilter === "pending" }, "Not in a leg yet") : null);
    toolbar.insertBefore(h("label", null, "Leg", select), toolbar.children[2] || null);
    const shown = commits.filter((c) => !legFilter || (legFilter === "pending" ? !c.leg_id : c.leg_id === legFilter));
    if (!shown.length) {
      stream.appendChild(h("div", { class: "empty" }, h("h2", null, "No commits here yet"),
        h("p", null, "Agents commit each logical change as they finish it; commits appear here with their messages as summaries.")));
      return;
    }
    const sessions = {};
    for (const s of feature.sessions) sessions[s.meta.session_id] = s.meta;
    const testIdx = {};
    for (const t of feature.evidence.tests || []) testIdx[t.id] = t;
    const ol = h("ol");
    outline.appendChild(ol);
    const observer = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
      for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); entry.target.dispatchEvent(new Event("load-diff")); }
    }, { rootMargin: "800px 0px" }) : null;
    shown.forEach((c, i) => {
      const seq = i + 1;
      const id = "commit-" + c.sha;
      ol.appendChild(h("li", null, h("a", { href: `#${id}`, onclick: (evt) => { evt.preventDefault(); document.getElementById(id).scrollIntoView({ block: "start" }); } },
        h("span", { class: "seq" }, String(seq)), h("span", { class: "label" }, c.subject || short(c.sha)))));
      const sess = c.session_id ? sessions[c.session_id] : null;
      const systems = c.systems || [];
      const tests = Array.from(new Set(feature.tests.tests.filter((t) => t.validates.some((v) => systems.includes(v.split("/")[0]))).map((t) => t.id)));
      const card = h("article", { class: ["card", "commit", "kind-" + c.kind], id },
        h("div", { class: "card-head" }, h("span", { class: "seq" }, String(seq)), h("h3", null, c.subject || "(no subject)"),
          h("a", { class: "sha", href: `#/commit/${c.sha}` }, short(c.sha))),
        messageBody(c.body),
        h("div", { class: "card-meta" },
          h("span", { class: ["kind-tag", c.kind] }, c.kind === "agent" ? `Agent commit${sess ? " · " + (sess.harness || "agent") : ""}` : c.kind === "developer" ? "Developer commit (outside any session)" : "Merge"),
          c.leg_id ? h("span", null, c.leg_id) : h("span", null, "not in a leg yet"),
          h("span", { class: "when" }, clock(c.committed_at)),
          systems.length ? h("span", { class: "chips" }, systems.map((sid) => h("a", { class: "chip system", href: `${base}/system/${enc(sid)}` }, sid))) : h("span", { class: "muted" }, "no system claims these lines"),
          tests.length ? h("span", { class: "chips" }, tests.map((tid) => h("a", { class: "chip test", href: `${base}/tests` }, tid, " ", testIdx[tid] ? (testIdx[tid].status === "verified_pass" ? "✓" : testIdx[tid].status === "verified_fail" ? "✗" : "?") : ""))) : null,
          c.thin_message ? h("span", { class: "ts-claimed_only" }, "thin message") : null,
          c.non_atomic ? h("span", { class: "ts-claimed_only" }, "spans unrelated systems") : null));
      const holder = h("div", { class: "commit-diff" }, h("p", { class: "loading small" }, "Loading changes…"));
      stream.appendChild(card);
      stream.appendChild(holder);
      let loaded = false;
      const load = async () => {
        if (loaded) return;
        loaded = true;
        try {
          const diff = await data.diff(feature.project.project_id, feature.feature_id, "commit:" + c.sha);
          clear(holder);
          const ctx = D.diffContext(feature, Object.assign({ idPrefix: `c${short(c.sha)}-` }, D.diffHooks ? D.diffHooks(feature, { commit: c.sha }) : {}));
          holder.appendChild(D.renderFiles(diff.files, ctx));
        } catch (err) { clear(holder).appendChild(D.errorBox(err)); }
      };
      holder.addEventListener("load-diff", load);
      if (observer) observer.observe(holder); else load();
    });
  }

  async function systemsMode(feature, route, outline, stream) {
    const q = route.query || {};
    const base = fpath(feature.project.project_id, feature.feature_id);
    stream.appendChild(h("p", { class: "loading" }, "Loading the feature diff…"));
    const diff = await data.diff(feature.project.project_id, feature.feature_id, "feature");
    clear(stream);
    const ctx = D.diffContext(feature, D.diffHooks ? D.diffHooks(feature) : {});
    const order = feature.systems.map((s) => s.id);
    const groups = new Map(order.map((sid) => [sid, []]));
    const extra = { incidental: [], weak: [], unclaimed: [] };
    const noise = [];
    for (const file of diff.files) {
      if (file.noise) { noise.push(file); continue; }
      const hunks = file.hunks.length ? file.hunks : [Object.assign({ id: "file-" + file.path, lines: [] }, file.file_unit || {})];
      for (const hunk of hunks) {
        const claimed = D.claimSystems(hunk).filter((sid) => groups.has(sid));
        let key = null;
        if (hunk.state === "covered" && claimed.length) key = claimed.sort((a, b) => order.indexOf(a) - order.indexOf(b))[0];
        if (key) groups.get(key).push([file, hunk]);
        else if (hunk.state === "covered") extra.incidental.push([file, hunk]); // explained by a test anchor
        else (extra[hunk.state] || extra.unclaimed).push([file, hunk]);
      }
    }
    const ul = h("ul");
    outline.appendChild(ul);
    const addOutline = (id, label, count, state) => ul.appendChild(h("li", null, h("a", { href: "#" + id,
      onclick: (evt) => { evt.preventDefault(); document.getElementById(id).scrollIntoView({ block: "start" }); } },
      glyph(state), h("span", { class: "label" }, label), h("span", { class: "seq" }, String(count)))));
    const renderGroup = (id, cls, head, items) => {
      const group = h("section", { class: ["system-group", cls], id });
      group.appendChild(head);
      const byFile = new Map();
      for (const [file, hunk] of items) {
        if (!byFile.has(file.path)) byFile.set(file.path, [file, []]);
        byFile.get(file.path)[1].push(hunk);
      }
      for (const [file, hunks] of byFile.values()) {
        const real = hunks.filter((hk) => hk.lines && hk.lines.length);
        group.appendChild(D.renderFile(file, ctx, real.length ? real : []));
      }
      stream.appendChild(group);
    };
    const linker = codeLinker(feature);
    const special = (key, cls, title, blurb, state) => {
      const items = extra[key];
      if (!items.length) return;
      const id = key === "unclaimed" ? "unexplained" : key;
      addOutline(id, title, items.length, state);
      renderGroup(id, cls, h("article", { class: "card" }, h("div", { class: "card-head" }, glyph(state), h("h3", null, title)),
        h("div", { class: "card-meta flush" }, blurb)), items);
    };
    // Unexplained changes lead: they are where review starts.
    special("unclaimed", "group-unclaimed", "Unexplained", "No system, test or incidental entry covers these changes.", "unclaimed");
    for (const sid of order) {
      const items = groups.get(sid);
      if (!items.length) continue;
      const system = feature.systems.find((s) => s.id === sid);
      const change = (system.sections.find((s) => s.title.toLowerCase() === "change") || {}).markdown || "";
      addOutline("sys-" + sid, system.title, items.length, "covered");
      renderGroup("sys-" + sid, null, h("article", { class: "card" },
        h("div", { class: "card-head" }, h("h3", null, h("a", { href: `${base}/system/${enc(sid)}` }, system.title)),
          h("span", { class: "chip" }, system.change || "touched"), h("span", { class: "sha" }, sid)),
        change ? h("div", { class: "card-prose" }, markdown(change, { linkCode: linker })) : h("div", { class: "card-meta flush" }, "No Change section."),
        system.critical_paths.length ? h("div", { class: "card-meta flush" }, `Critical paths: ${system.critical_paths.map((cp) => cp.id).join(", ")}`) : null), items);
    }
    special("weak", "group-weak", "Weak claims", "Only a path anchor points here: no symbol or line range explains these lines.", "weak");
    special("incidental", "group-incidental", "Incidental", "Changed but not worth a system: listed in the brief, or explained only by a test.", "incidental");
    const unexplained = document.getElementById("unexplained");
    if (noise.length) {
      const row = h("div", { class: "noise-row" }, `${plural(noise.length, "generated, vendored or lock file")} collapsed`,
        h("button", { class: "btn small", type: "button", onclick: () => { row.replaceWith(D.renderFiles(noise.map((f) => Object.assign({}, f, { noise: null })), ctx)); } }, "Show them"));
      stream.appendChild(row);
    }
    if (!diff.files.length) stream.appendChild(h("div", { class: "empty" }, h("h2", null, "No changes yet"), h("p", null, "The branch has no changes against its base.")));
    const target = q.hunk ? document.getElementById("hunk-" + q.hunk) : q.path ? document.getElementById("file-" + encodeURIComponent(q.path)) : null;
    if (target) {
      target.classList.add("current");
      target.scrollIntoView({ block: "start" });
      if (target.focus) target.focus({ preventScroll: true });
    } else if (location.hash.endsWith("#unexplained") && unexplained) {
      unexplained.scrollIntoView({ block: "start" });
    }
  }

  // --- timeline ------------------------------------------------------------------------------------------

  const JOURNAL_KINDS = ["plan", "decision", "finding", "change", "test", "blocker", "handoff"];

  async function viewTimeline(root, feature) {
    const base = fpath(feature.project.project_id, feature.feature_id);
    const hidden = new Set((D.prefs().timelineHidden || []));
    const filters = h("div", { class: "filters", role: "group", "aria-label": "Show entries" });
    const kinds = JOURNAL_KINDS.concat(["commit", "run", "session"]);
    const body = h("div");
    const draw = () => {
      clear(filters);
      for (const k of kinds) {
        filters.appendChild(h("button", { type: "button", "aria-pressed": String(!hidden.has(k)), onclick: () => {
          if (hidden.has(k)) hidden.delete(k); else hidden.add(k);
          D.savePref("timelineHidden", Array.from(hidden));
          draw();
        } }, k));
      }
      clear(body);
      const legs = feature.legs.length ? feature.legs : [{ leg_id: "leg-01", sessions: [] }];
      const commitsByLeg = {};
      for (const c of feature.evidence.commits || []) (commitsByLeg[c.leg_id || "pending"] = commitsByLeg[c.leg_id || "pending"] || []).push(c);
      for (const leg of legs.concat(commitsByLeg.pending ? [{ leg_id: "pending", pseudo: true }] : [])) {
        const rows = [];
        for (const s of feature.sessions.filter((x) => x.meta.leg_id === leg.leg_id)) {
          const m = s.meta;
          rows.push({ at: m.started_at, kind: "session", el: [h("span", { class: "k" }, "session"),
            h("span", { class: "body" }, `${m.harness || "agent"} on ${m.host || "?"}: ${m.task || "no task given"}`,
              h("span", { class: "muted" }, ` (${m.status}${m.ended_at ? ", ended " + clock(m.ended_at) : ""})`))], cls: "session-row" });
          for (const e of s.journal) {
            rows.push({ at: e.at, kind: e.kind, el: [h("span", { class: ["k", e.kind] }, e.kind), h("div", { class: "body" }, markdown(e.text))] });
          }
          for (const r of s.runs) {
            rows.push({ at: r.started_at, kind: "run", el: [h("span", { class: "k" }, "run"),
              h("span", { class: ["body", "mono", r.exit_code !== 0 && "run-fail"] }, `${r.command}  →  ${r.exit_code === 0 ? "passed" : "exit " + r.exit_code} in ${Math.round(r.duration_s || 0)} s`)] });
          }
        }
        for (const c of commitsByLeg[leg.leg_id] || []) {
          rows.push({ at: c.committed_at, kind: "commit", el: [h("span", { class: ["k", "commit-" + c.kind] }, c.kind === "developer" ? "dev commit" : "commit"),
            h("span", { class: "body" }, h("a", { href: `#/commit/${c.sha}`, class: "mono small" }, short(c.sha)), "  ", c.subject)] });
        }
        rows.sort((a, b) => (D.parseTime(a.at) || 0) - (D.parseTime(b.at) || 0));
        const ev = leg.evidence || {};
        const section = h("section", { class: "tl-leg", id: "leg-" + leg.leg_id },
          h("div", { class: "tl-leg-head" }, h("h2", null, leg.pseudo ? "Not in a leg yet" : leg.leg_id),
            leg.pseudo ? h("span", { class: "muted" }, "Commits after the last closed leg") :
              h("span", { class: "muted" }, leg.closed_at ? `closed ${clock(leg.closed_at)}` : "open",
                ev.base ? ` · ${short(ev.base)}..${ev.head === "WORKTREE" ? "worktree" : short(ev.head)}` : ""),
            leg.pseudo ? null : h("a", { href: `${base}/diff?mode=story&leg=${enc(leg.leg_id)}` }, "Story for this leg")));
        const list = h("ol", { class: "tl" });
        for (const row of rows) {
          if (hidden.has(row.kind)) continue;
          list.appendChild(h("li", { class: row.cls }, h("span", { class: "t" }, clock(row.at)), row.el));
        }
        if (!list.children.length) list.appendChild(h("li", null, h("span", { class: "t" }), h("span", { class: "k" }), h("span", { class: "muted" }, "Nothing to show with these filters.")));
        section.appendChild(list);
        body.appendChild(section);
      }
    };
    root.appendChild(filters);
    root.appendChild(body);
    draw();
    const anchor = location.hash.split("#")[2];
    if (anchor) { const el = document.getElementById(anchor); if (el) el.scrollIntoView(); }
  }

  // --- commit page --------------------------------------------------------------------------------------

  async function viewCommit(root, route) {
    loading(root);
    const info = await data.commit(route.sha);
    clear(root);
    D.setCrumbs([["Commit " + short(info.sha), null]]);
    for (const lf of info.landed) {
      root.appendChild(h("article", { class: "card commit" },
        h("div", { class: "card-head" }, h("h3", null, `Squash commit ${short(lf.sha)} landed `, h("a", { href: fpath(lf.project_id, lf.feature_id) }, lf.title))),
        h("div", { class: "card-meta" }, `Matched by ${lf.method === "tree" ? "identical tree" : lf.method === "patch-id" ? "identical patch" : "matching files"} (${Math.round((lf.confidence || 0) * 100)}%). ${lf.detail || ""}`),
        h("div", { class: "card-meta" }, (lf.legs || []).map((leg) => h("span", null, `${leg.leg_id}: `,
          (leg.commits || []).map((sha) => h("a", { class: "mono small", href: `#/commit/${sha}` }, short(sha) + " ")))))));
    }
    for (const fc of info.features) {
      const c = fc.commit;
      const feature = await data.feature(fc.project_id, fc.feature_id).catch(() => null);
      root.appendChild(h("p", null, h("a", { href: fpath(fc.project_id, fc.feature_id) }, fc.title),
        h("span", { class: "muted" }, `  ${c.leg_id || "not in a leg yet"}`)));
      root.appendChild(h("article", { class: ["card", "commit", "flush", "kind-" + c.kind] },
        h("div", { class: "card-head" }, h("h3", null, c.subject || "(no subject)"), h("span", { class: "sha" }, c.sha)),
        messageBody(c.body),
        h("div", { class: "card-meta" }, h("span", { class: ["kind-tag", c.kind] }, c.kind === "agent" ? "Agent commit" : c.kind === "developer" ? "Developer commit" : "Merge"),
          h("span", null, c.author || ""), h("span", { class: "when" }, clock(c.committed_at)),
          (c.systems || []).length ? h("span", { class: "chips" }, c.systems.map((sid) => h("a", { class: "chip system", href: `${fpath(fc.project_id, fc.feature_id)}/system/${enc(sid)}` }, sid))) : null,
          c.rebased_from ? h("span", { class: "muted" }, `rebased from ${short(c.rebased_from)}`) : null)));
      const ctx = feature ? D.diffContext(feature, D.diffHooks ? D.diffHooks(feature, { commit: c.sha }) : {}) : { pid: fc.project_id, fid: fc.feature_id };
      root.appendChild(D.renderFiles(fc.diff.files, ctx));
      const journal = fc.journal.slice().sort((a, b) => (D.parseTime(a.at) || 0) - (D.parseTime(b.at) || 0));
      if (journal.length || fc.runs.length) {
        root.appendChild(sectionTitle(`Journal and runs for ${c.leg_id || "this commit"}`));
        const list = h("ol", { class: "tl" });
        for (const e of journal) list.appendChild(h("li", null, h("span", { class: "t" }, clock(e.at)), h("span", { class: ["k", e.kind] }, e.kind), h("div", { class: "body" }, markdown(e.text))));
        for (const r of fc.runs) list.appendChild(h("li", null, h("span", { class: "t" }, clock(r.started_at)), h("span", { class: "k" }, "run"),
          h("span", { class: ["body", "mono", r.exit_code !== 0 && "run-fail"] }, `${r.command}  →  ${r.exit_code === 0 ? "passed" : "exit " + r.exit_code}`)));
        root.appendChild(list);
      }
    }
  }

  // --- search -------------------------------------------------------------------------------------------

  async function viewSearch(root, route) {
    const q = (route.query && route.query.q) || "";
    D.setCrumbs([["Search", null]]);
    const input = h("input", { type: "search", value: q, placeholder: "Systems, files, symbols, journal text", "aria-label": "Search" });
    const form = h("form", { class: "search-page", onsubmit: (evt) => { evt.preventDefault(); location.hash = `#/search?q=${enc(input.value)}`; } },
      h("h1", null, "Search"), input);
    clear(root).appendChild(form);
    if (!q) { input.focus(); return; }
    const results = h("ul", { class: "results" }, h("li", { class: "loading" }, "Searching…"));
    root.appendChild(results);
    const res = await data.search(q);
    clear(results);
    if (!res.results.length) {
      results.appendChild(h("li", { class: "muted" }, `Nothing matches “${q}”. Search covers systems, files, symbols, decisions, tests, commits and journals.`));
      return;
    }
    const kindWord = { system: "System", file: "File", decision: "Decision", test: "Test", brief: "Brief", commit: "Commit", journal: "Journal" };
    for (const r of res.results) {
      const base = fpath(r.project_id, r.feature_id);
      const href = r.kind === "system" ? `${base}/system/${enc(r.ref)}` : r.kind === "file" ? `${base}/diff?mode=systems&path=${enc(r.ref)}`
        : r.kind === "decision" ? `${base}/decision/${enc(r.ref)}` : r.kind === "test" ? `${base}/tests` : r.kind === "commit" ? `#/commit/${r.ref}`
          : r.kind === "journal" ? `${base}/timeline` : base;
      results.appendChild(h("li", null, h("span", { class: "r-kind" }, kindWord[r.kind] || r.kind),
        h("a", { class: "r-title", href }, r.kind === "file" ? r.ref : r.title || r.ref),
        h("div", { class: "r-where" }, `${r.feature_title || r.feature_id}`),
        r.snippet ? h("div", { class: "r-snippet" }, r.snippet) : null));
    }
    input.focus();
  }

  // --- epic ---------------------------------------------------------------------------------------------

  async function viewEpic(root, route) {
    loading(root);
    const epic = await data.epic(route.name);
    D.setCrumbs([["Epic " + epic.epic, null]]);
    clear(root);
    root.appendChild(h("h1", null, `Epic ${epic.epic}`));
    root.appendChild(h("p", { class: "muted" }, `${plural(epic.features.length, "feature")}, in the order work started.`));
    const list = h("ol", { class: "tl" });
    epic.features.forEach((f) => {
      list.appendChild(h("li", null, h("span", { class: "t" }, clock(f.started_at)), h("span", null, statusPill(f.status)),
        h("div", null, h("a", { href: fpath(f.project_id, f.feature_id) }, h("strong", null, f.title)),
          h("span", { class: "muted small" }, `  ${pct(f.coverage)} explained`),
          f.intent ? h("div", { class: "small muted" }, "Intent") : null, f.intent ? markdown(f.intent) : null,
          f.outcome ? h("div", { class: "small muted" }, "Outcome") : null, f.outcome ? markdown(f.outcome) : null)));
    });
    root.appendChild(list);
    const seen = new Map();
    for (const f of epic.features) for (const s of f.systems) if (!seen.has(s.id)) seen.set(s.id, Object.assign({ feature: f }, s));
    if (seen.size) {
      root.appendChild(sectionTitle("Systems across the epic", plural(seen.size, "system")));
      root.appendChild(mapLegend());
      root.appendChild(h("div", { class: "map-wrap" }, systemMap(Array.from(seen.values()), {
        onPick: (sid) => { const s = seen.get(sid); location.hash = `${fpath(s.feature.project_id, s.feature.feature_id)}/system/${enc(sid)}`; } })));
    }
  }

  Object.assign(D, { viewIndex, viewFeature, viewCommit, viewSearch, viewEpic, systemMap, statusPill, sectionTitle, codeLinker, fpath });
})();
