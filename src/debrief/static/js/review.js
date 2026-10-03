/* Debrief viewer: the review loop. Line comments, review marks, prompts for the
 * agent, Close leg, export, and a notice when the watcher brings new evidence. */
(function () {
  "use strict";
  const D = window.Debrief;
  const { h, clear, data, markdown, ago, plural } = D;
  const enc = encodeURIComponent;

  const readonly = () => Boolean(D.exported);
  // Whether this viewer may change review records: not an export, not a read-only server, not a hub reader.
  const canWrite = (feature) => !D.exported && !(D.meta && D.meta.readonly) && !(feature && feature.role === "reader");
  const me = (feature) => (feature && feature.review && feature.review.user) || (D.meta && D.meta.user && D.meta.user.name) || null;
  const isMine = (feature, comment) => !comment.author || comment.author === me(feature);
  D.canWrite = canWrite;
  const fbase = (feature) => data.featurePath(feature.project.project_id, feature.feature_id);

  // --- comments in the diff -------------------------------------------------------------------

  function commentKey(path, side, line) { return `${path}|${side}|${line}`; }

  function indexComments(feature, scope) {
    const out = new Map();
    const byBlob = new Map();
    const add = (map, key, c) => { if (!map.has(key)) map.set(key, []); map.get(key).push(c); };
    for (const c of feature.comments || []) {
      const a = c.anchor || {};
      let key = null;
      if (scope === "feature") {
        if (a.side === "new" && c.current && !c.current.outdated && c.current.line) key = commentKey(a.path, "new", c.current.line);
        else if (a.side === "old" && (!a.scope || a.scope === "feature") && c.current && !c.current.outdated) key = commentKey(a.path, "old", a.line);
      } else if (a.scope === scope) {
        key = commentKey(a.path, a.side, a.line);
      }
      if (key) add(out, key, c);
      // Any diff showing the exact file version a comment was made on can show it too, whichever
      // mode it was made in: a Systems-mode comment appears on the commit that wrote that version.
      if (a.blob) add(byBlob, commentKey(a.blob, a.side, a.line), c);
    }
    return { get(path, blob, side, line) {
      const found = (out.get(commentKey(path, side, line)) || []).slice();
      for (const c of (blob && byBlob.get(commentKey(blob, side, line))) || []) if (!found.includes(c)) found.push(c);
      return found.length ? found : null;
    } };
  }

  const STATE_WORD = { open: "Open", sent: "Sent to the agent", resolved: "Resolved" };

  function commentRow(feature, comment, onChange) {
    const row = h("tr", { class: "comment-row" });
    const cell = h("td", { colspan: 3 });
    row.appendChild(cell);
    const draw = () => {
      clear(cell);
      cell.appendChild(commentCard(feature, comment, () => { draw(); if (onChange) onChange(); }, () => row.remove()));
    };
    draw();
    return row;
  }

  function commentCard(feature, comment, redraw, removed) {
    const base = fbase(feature);
    const card = h("div", { class: ["comment", "state-" + comment.state, comment.visibility === "shared" && "shared"] });
    const act = async (patch) => {
      try {
        const res = await data.patch(`${base}/comments/${comment.id}`, patch);
        Object.assign(comment, res.comment);
        redraw();
      } catch (err) { D.toast(err.message); }
    };
    // Only the author edits, shares or deletes; anyone who may write can resolve a shared comment.
    const mine = isMine(feature, comment);
    const mayResolve = canWrite(feature) && (mine || comment.visibility === "shared");
    const actions = [
      mayResolve ? (comment.state === "resolved"
        ? h("button", { class: "btn small", type: "button", onclick: () => act({ state: "open" }) }, "Reopen")
        : h("button", { class: "btn small", type: "button", onclick: () => act({ state: "resolved" }) }, "Resolve")) : null,
      canWrite(feature) && mine ? h("button", { class: "btn small", type: "button", onclick: () => {
        clear(card);
        card.appendChild(composer({ body: comment.body, visibility: comment.visibility, submitLabel: "Save changes",
          onCancel: redraw,
          onSubmit: async (body, visibility) => { await act({ body, visibility }); } }));
      } }, "Edit") : null,
      canWrite(feature) && mine ? h("button", { class: "btn small", type: "button", onclick: async () => {
        if (!window.confirm("Delete this comment?")) return;
        try {
          await data.del(`${base}/comments/${comment.id}`);
          feature.comments = (feature.comments || []).filter((c) => c.id !== comment.id);
          removed();
          D.toast("Comment deleted");
        } catch (err) { D.toast(err.message); }
      } }, "Delete") : null,
    ];
    card.appendChild(h("div", { class: "comment-meta" },
      h("strong", null, comment.author || "you"),
      h("span", { class: "when" }, ago(comment.updated_at || comment.created_at)),
      h("span", { class: "chip" + (comment.visibility === "shared" ? " system" : "") },
        comment.visibility === "shared" ? "Shared" : "Private"),
      h("span", { class: "chip muted" }, STATE_WORD[comment.state] || comment.state),
      comment.current && comment.current.outdated ? h("span", { class: "chip", title: "The line changed after this comment" }, "Outdated") : null,
      h("span", { class: "spacer" }), actions));
    card.appendChild(markdown(comment.body));
    if (comment.current && comment.current.outdated && comment.state !== "resolved" && mayResolve) {
      // The agent changed this code after the comment: offer to close the thread.
      card.appendChild(h("p", { class: "small outdated-note" }, "This code changed after the comment. ",
        h("button", { class: "btn small", type: "button", onclick: () => act({ state: "resolved" }) }, "Resolve it"),
        " if the change answers it."));
    }
    if (comment.resolved_by && comment.state === "resolved") {
      card.appendChild(h("p", { class: "small muted" }, `Resolved by ${comment.resolved_by}`));
    }
    for (const reply of comment.replies || []) {
      card.appendChild(h("div", { class: "reply" }, h("div", { class: "comment-meta" }, h("strong", null, reply.author), h("span", { class: "when" }, ago(reply.created_at))),
        markdown(reply.body)));
    }
    if (comment.visibility === "shared" && canWrite(feature)) {
      const replyBtn = h("button", { class: "btn small reply-btn", type: "button", onclick: () => {
        replyBtn.replaceWith(composer({ submitLabel: "Reply", hideVisibility: true, onCancel: redraw,
          onSubmit: async (body) => { await act({ reply: body }); } }));
      } }, "Reply");
      card.appendChild(replyBtn);
    }
    return card;
  }

  function composer(opts) {
    const textarea = h("textarea", { rows: 3, placeholder: "Note for yourself, or feedback for the agent", "aria-label": "Comment" });
    textarea.value = opts.body || "";
    const hub = D.meta && D.meta.mode === "hub";
    const visibility = h("select", { "aria-label": "Who can see this", hidden: opts.hideVisibility || null },
      h("option", { value: "private" }, hub ? "Private: only you see it" : "Private: stays on this machine"),
      h("option", { value: "shared" }, hub ? "Shared: the team and the agent's host see it" : "Shared: syncs with the project's records"));
    visibility.value = opts.visibility || D.prefs().commentVisibility || "private";
    const submit = async (evt) => {
      if (evt) evt.preventDefault();
      const body = textarea.value.trim();
      if (!body) { textarea.focus(); return; }
      button.disabled = true;
      try {
        D.savePref("commentVisibility", visibility.value);
        await opts.onSubmit(body, visibility.value);
      } catch (err) {
        D.toast(err.message);
        button.disabled = false;
      }
    };
    const button = h("button", { class: "btn primary small", type: "submit" }, opts.submitLabel || "Comment");
    textarea.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter" && (evt.metaKey || evt.ctrlKey)) submit(evt);
      if (evt.key === "Escape") opts.onCancel();
    });
    const form = h("form", { class: "composer", onsubmit: submit }, textarea,
      h("div", { class: "composer-actions" }, visibility, h("span", { class: "spacer" }),
        h("button", { class: "btn small", type: "button", onclick: () => opts.onCancel() }, "Cancel"), button));
    setTimeout(() => textarea.focus(), 0);
    return form;
  }

  function openComposer(feature, tr, file, hunk, tag, o, n, text, scope, commit) {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("composer-row")) { next.querySelector("textarea").focus(); return; }
    const side = tag === "-" ? "old" : "new";
    const line = side === "old" ? o : n;
    const row = h("tr", { class: "composer-row" });
    const cell = h("td", { colspan: 3 });
    row.appendChild(cell);
    cell.appendChild(composer({
      onCancel: () => row.remove(),
      onSubmit: async (body, visibility) => {
        const anchor = {
          commit: scope === "feature" ? (feature.evidence.head_commit || feature.evidence.head) : commit,
          scope, path: side === "old" ? (file.old_path || file.path) : file.path, side, line, text,
          blob: side === "old" ? file.old_blob : file.new_blob,
        };
        const res = await data.post(`${fbase(feature)}/comments`, { anchor, body, visibility });
        const comment = Object.assign(res.comment, { current: { line, outdated: false, moved: false } });
        feature.comments = (feature.comments || []).concat([comment]);
        row.replaceWith(commentRow(feature, comment));
        D.toast(visibility === "shared" ? "Comment saved and shared" : "Comment saved");
        refreshTabCount(feature);
      },
    }));
    tr.after(row);
    // Focus now, inside the tap that opened it: iOS raises the keyboard only for focus given during a gesture.
    row.querySelector("textarea").focus({ preventScroll: true });
  }

  function refreshTabCount(feature) {
    const open = (feature.comments || []).filter((c) => c.state === "open").length;
    const tab = document.querySelector('.tabs a[href$="/comments"] .count');
    if (tab) tab.textContent = String(open);
  }

  // --- review marks ---------------------------------------------------------------------------------

  function markSet(feature) {
    if (!feature._marks) feature._marks = new Set(feature.marks || []);
    return feature._marks;
  }

  // Marks are private to the reviewer, so hub readers keep them; read-only copies don't.
  const canMark = () => !D.exported && !(D.meta && D.meta.readonly);

  async function toggleMark(feature, hunkId, button) {
    if (!canMark()) return;
    const marks = markSet(feature);
    const reviewed = !marks.has(hunkId);
    try {
      await data.post(`${fbase(feature)}/marks`, { hunk_id: hunkId, reviewed });
    } catch (err) { D.toast(err.message); return; }
    if (reviewed) marks.add(hunkId); else marks.delete(hunkId);
    for (const btn of document.querySelectorAll(`.mark-btn[data-hunk="${hunkId}"]`)) setMarkButton(btn, reviewed);
    for (const cell of document.querySelectorAll(`.strip .cell[data-hunk="${hunkId}"]`)) cell.classList.toggle("reviewed", reviewed);
    if (feature.review) feature.review.reviewed = marks.size;
    const progress = document.querySelector(".review-progress");
    if (progress && feature.review) progress.textContent = progressText(feature);
    if (button) button.focus();
  }

  function setMarkButton(btn, reviewed) {
    btn.setAttribute("aria-pressed", String(reviewed));
    btn.textContent = reviewed ? "Reviewed" : "Mark reviewed";
  }

  function progressText(feature) {
    const r = feature.review || { reviewed: 0, total: 0 };
    return `You've reviewed ${r.reviewed} of ${plural(r.total, "hunk")}`;
  }

  // --- hooks used by the diff and the pages -----------------------------------------------------

  D.diffHooks = function (feature, opts) {
    if (!feature) return {};
    const commit = opts && opts.commit;
    const scope = commit ? "commit:" + commit : "feature";
    const comments = indexComments(feature, scope);
    return {
      decorateLine(tr, file, hunk, tag, o, n, text) {
        if (tag === "\\") return null;
        const side = tag === "-" ? "old" : "new";
        const line = side === "old" ? o : n;
        if (canWrite(feature)) {
          const numbers = tr.querySelectorAll("td.ln > span");
          const target = side === "old" ? numbers[0] : numbers[1];
          if (target && line) {
            const open = () => openComposer(feature, tr, file, hunk, tag, o, n, text, scope, commit);
            const btn = h("button", { class: "ln-btn", type: "button", title: "Comment on this line",
              "aria-label": `Comment on ${side === "old" ? "removed " : ""}line ${line} of ${file.path}` }, String(line));
            btn.addEventListener("click", open);
            clear(target).appendChild(btn);
            tr._comment = open; // the touch review bar comments on a picked line through this
          }
        }
        const path = side === "old" ? (file.old_path || file.path) : file.path;
        const found = comments.get(path, side === "old" ? file.old_blob : file.new_blob, side, line);
        return found ? found.map((c) => commentRow(feature, c)) : null;
      },
      hunkActions(file, hunk) {
        if (!canMark() || !hunk.id || scope !== "feature") return null;
        const btn = h("button", { class: "btn small mark-btn", type: "button", dataset: { hunk: hunk.id } });
        setMarkButton(btn, markSet(feature).has(hunk.id));
        btn.addEventListener("click", () => toggleMark(feature, hunk.id, btn));
        btn._toggle = () => toggleMark(feature, hunk.id, null); // for the touch review bar, without moving focus
        return btn;
      },
    };
  };

  D.reviewedSet = (feature) => markSet(feature);

  D.headerExtras = function (feature) {
    if (!feature.review || readonly()) return null;
    return h("span", { class: "review-progress" }, progressText(feature));
  };

  D.extraTabs = [["comments", "Comments"]];
  D.extraTabCounts = (feature) => {
    const open = (feature.comments || []).filter((c) => c.state === "open").length;
    return { comments: h("span", { class: "count" }, String(open)) };
  };

  // --- Close leg and export on the brief ---------------------------------------------------------

  D.legActions = function (block, feature) {
    const last = feature.legs[feature.legs.length - 1];
    if (!last || last.closed_at || !canWrite(feature)) return;
    const holder = h("div", { class: "prompt-box" });
    const show = (res) => {
      clear(holder);
      holder.appendChild(h("p", { class: "small" }, `Close requested for ${res.leg_id}. The agent sees it at its next `,
        h("code", null, "bin/session now"), "; or paste this into the agent:"));
      const text = h("textarea", { class: "prompt-text", rows: 3, readonly: true, "aria-label": "Prompt for the agent" });
      text.value = res.prompt;
      holder.appendChild(text);
      holder.appendChild(h("div", { class: "dialog-actions" }, h("button", { class: "btn small", type: "button", onclick: () => D.copyText(res.prompt) }, "Copy prompt")));
    };
    const request = async (note) => {
      try {
        const res = await data.post(`${fbase(feature)}/close-request`, note ? { note } : {});
        last.close_requested_at = res.close_requested_at;
        show(res);
      } catch (err) { D.toast(err.message); }
    };
    if (last.close_requested_at) {
      holder.appendChild(h("button", { class: "btn small", type: "button", onclick: () => request() }, "Show the close prompt"));
    } else {
      holder.appendChild(h("p", { class: "small muted" }, `Closing ${last.leg_id} asks the agent to write its records against the final code and publish them.`));
      holder.appendChild(h("button", { class: "btn primary small", type: "button", onclick: () => request() }, `Close ${last.leg_id}`));
    }
    block.appendChild(holder);
  };

  D.briefExtras = function (aside, feature) {
    if (readonly()) return;
    aside.appendChild(h("div", { class: "aside-block" }, h("h3", null, "Share"),
      h("p", { class: "small" }, "One HTML file with this feature's records, diffs and shared comments, for a PR or a teammate without Debrief. It contains code."),
      h("a", { class: "btn small", href: `${fbase(feature)}/export`, download: `debrief-${feature.feature_id}.html` }, "Download as one HTML file")));
  };

  // --- the Comments tab ---------------------------------------------------------------------------

  async function viewComments(root, feature) {
    const all = feature.comments || [];
    if (!all.length) {
      root.appendChild(h("div", { class: "empty" }, h("h2", null, "No comments yet"),
        h("p", null, readonly() ? "This export has no shared comments." :
          "Click a line number in the diff to leave a note. Notes stay private unless you share them; turn open notes into a prompt for the agent here.")));
      return;
    }
    let filter = all.some((c) => c.state === "open") ? "open" : "all";
    const selected = new Set();
    const toolbar = h("div", { class: "diff-toolbar" });
    const list = h("div");
    const draw = () => {
      clear(toolbar);
      const counts = { open: 0, sent: 0, resolved: 0 };
      for (const c of all) counts[c.state] = (counts[c.state] || 0) + 1;
      toolbar.appendChild(h("div", { class: "filters", role: "group", "aria-label": "Show comments" },
        [["open", `Open ${counts.open}`], ["sent", `Sent ${counts.sent}`], ["resolved", `Resolved ${counts.resolved}`], ["all", `All ${all.length}`]]
          .map(([id, label]) => h("button", { type: "button", "aria-pressed": String(filter === id), onclick: () => { filter = id; draw(); } }, label))));
      if (canWrite(feature)) {
        toolbar.appendChild(h("span", { class: "spacer" }));
        toolbar.appendChild(h("button", { class: "btn primary", type: "button", disabled: !selected.size && !counts.open,
          title: !selected.size && !counts.open ? "Select comments to include, or leave some open" : null,
          onclick: () => generate(feature, selected.size ? Array.from(selected) : null) },
          selected.size ? `Generate prompt from ${plural(selected.size, "comment")}` : "Generate prompt from open comments"));
      }
      clear(list);
      const shown = all.filter((c) => filter === "all" || c.state === filter);
      if (!shown.length) list.appendChild(h("p", { class: "muted" }, "Nothing here."));
      const byPath = new Map();
      for (const c of shown) {
        const p = (c.anchor || {}).path || "";
        if (!byPath.has(p)) byPath.set(p, []);
        byPath.get(p).push(c);
      }
      for (const [path, items] of byPath) {
        list.appendChild(D.sectionTitle(path, plural(items.length, "comment"), "h3"));
        for (const c of items) {
          const a = c.anchor || {};
          const cur = c.current || {};
          const where = a.side === "old" ? `removed line ${a.line}` : cur.outdated ? `was line ${a.line}` : `line ${cur.line || a.line}`;
          const check = !canWrite(feature) ? null : h("input", { type: "checkbox", "aria-label": "Include in the prompt", checked: selected.has(c.id),
            onchange: (evt) => { if (evt.currentTarget.checked) selected.add(c.id); else selected.delete(c.id); draw(); } });
          const href = `#/p/${enc(feature.project.project_id)}/f/${enc(feature.feature_id)}/diff?mode=systems&path=${enc(path)}`;
          const item = h("div", { class: "comment-item" }, check,
            h("div", { class: "comment-main" },
              h("div", { class: "small" }, h("a", { href }, where), cur.moved ? h("span", { class: "muted" }, ` (moved from line ${a.line})`) : null,
                cur.outdated && a.side !== "old" ? h("span", { class: "ts-claimed_only" }, " (the line has changed since)") : null),
              a.text ? h("pre", { class: "quoted" }, a.text) : null,
              commentCard(feature, c, draw, () => { all.splice(all.indexOf(c), 1); draw(); })));
          list.appendChild(item);
        }
      }
    };
    root.appendChild(toolbar);
    root.appendChild(list);
    draw();
  }

  async function generate(feature, ids) {
    let res;
    try {
      res = await data.post(`${fbase(feature)}/prompt`, ids ? { ids } : {});
    } catch (err) { D.toast(err.message); return; }
    for (const c of feature.comments || []) if (res.ids.includes(c.id) && c.state === "open") c.state = "sent";
    const text = h("textarea", { class: "prompt-text", rows: 14, readonly: true, "aria-label": "Prompt" });
    text.value = res.prompt;
    const queued = h("p", { class: "small muted" }, "Copy it into the agent's chat, or queue it: ", h("code", null, "bin/session now"),
      " relays queued feedback at the agent's next journal entry.");
    const dlg = h("dialog", { "aria-labelledby": "prompt-title" },
      h("h2", { id: "prompt-title" }, `Prompt from ${plural(res.count, "comment")}`), text, queued,
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn", type: "button", onclick: () => D.download(`review-${feature.feature_id}.md`, res.prompt) }, "Download .md"),
        h("button", { class: "btn", type: "button", onclick: async (evt) => {
          evt.currentTarget.disabled = true;
          try {
            await data.post(`${fbase(feature)}/prompt`, { ids: res.ids, queue: true });
            clear(queued).appendChild(document.createTextNode("Queued in feedback.md. The agent sees it at its next journal entry."));
          } catch (err) { D.toast(err.message); evt.currentTarget.disabled = false; }
        } }, "Queue for the agent"),
        h("button", { class: "btn primary", type: "button", onclick: () => D.copyText(res.prompt) }, "Copy prompt"),
        h("button", { class: "btn", type: "button", onclick: () => dlg.close() }, "Close")));
    dlg.addEventListener("close", () => { dlg.remove(); D.rerender(); });
    document.body.appendChild(dlg);
    dlg.showModal();
    text.select();
  }

  D.extraFeatureViews = { comments: viewComments };

  // --- keyboard -----------------------------------------------------------------------------------
  D.extraKeys = [["m", "Mark the current hunk reviewed"], ["c", "Comment on the current hunk"]];
  D.onKey = function (evt) {
    const current = document.querySelector(".hunk.current");
    if (!current) return;
    if (evt.key === "m") {
      const btn = current.querySelector(".mark-btn");
      if (btn) { btn.click(); evt.preventDefault(); }
    } else if (evt.key === "c") {
      const btn = current.querySelector("tr.add .ln-btn") || current.querySelector(".ln-btn");
      if (btn) { btn.click(); evt.preventDefault(); }
    }
  };

  // --- touch review bar -------------------------------------------------------------------------------
  // A phone or tablet has no j, k, c or m keys. Where the pointer is a finger (or the screen is phone
  // sized), pages with hunks get a bar within thumb reach: previous and next hunk, comment on the
  // picked line (tap a code line to pick it) or the hunk's first change, and mark the hunk reviewed.
  // It acts on the current hunk, outlined; scrolling makes the hunk at the top of the screen current.
  const touchQuery = window.matchMedia ? window.matchMedia("(pointer: coarse), (max-width: 640px)") : null;
  const touch = () => Boolean(touchQuery && touchQuery.matches);
  const TOP_BAND = 64; // sticky tab bar plus a little air
  let bar = null;
  let picked = null;

  function hunkAtTop() {
    for (const el of document.querySelectorAll("#app .hunk")) {
      const r = el.getBoundingClientRect();
      if (r.bottom > TOP_BAND + 24) return r.top < window.innerHeight ? el : null;
    }
    return null;
  }

  function currentHunk() {
    const current = document.querySelector("#app .hunk.current");
    return current && D.onScreen(current) ? current : hunkAtTop();
  }

  function setCurrent(hunk) {
    for (const el of document.querySelectorAll(".hunk.current")) if (el !== hunk) el.classList.remove("current");
    if (hunk) hunk.classList.add("current");
  }

  function unpick() {
    if (picked) picked.classList.remove("picked");
    picked = null;
  }

  function buildBar() {
    const comment = h("button", { class: "btn", type: "button", onclick: () => {
      const row = picked && picked.isConnected ? picked : null;
      if (row) { setCurrent(row.closest(".hunk")); unpick(); row._comment(); return sync(); }
      const hunk = currentHunk();
      const first = hunk && (Array.from(hunk.querySelectorAll("tr.add, tr.del")).find((tr) => tr._comment) ||
        Array.from(hunk.querySelectorAll("tr")).find((tr) => tr._comment));
      if (first) { setCurrent(hunk); first._comment(); }
      sync();
    } }, "Comment");
    const mark = h("button", { class: "btn mark-toggle", type: "button", "aria-pressed": "false", onclick: async () => {
      const hunk = currentHunk();
      const btn = hunk && hunk.querySelector(".mark-btn");
      if (!btn) return;
      setCurrent(hunk);
      await btn._toggle();
      sync();
    } }, h("span", { class: "box", "aria-hidden": "true" }, D.icon("check")), "Reviewed");
    bar = h("div", { class: "review-bar", role: "toolbar", "aria-label": "Review", hidden: true },
      h("button", { class: "iconbtn", type: "button", "aria-label": "Previous hunk", onclick: () => { unpick(); D.moveHunk(-1); sync(); } }, D.icon("up")),
      h("button", { class: "iconbtn", type: "button", "aria-label": "Next hunk", onclick: () => { unpick(); D.moveHunk(1); sync(); } }, D.icon("down")),
      h("span", { class: "spacer" }), comment, mark);
    bar.parts = { comment, mark };
    document.body.appendChild(bar);
  }

  function sync() {
    if (!bar) buildBar();
    const show = touch() && Boolean(document.querySelector("#app .hunk"));
    bar.hidden = !show;
    document.body.classList.toggle("has-review-bar", show);
    if (!show) return;
    // A saved comment removes its focused field without a focusout, so check what has focus now.
    bar.classList.toggle("typing", Boolean(document.activeElement) && typing(document.activeElement));
    if (picked && !picked.isConnected) picked = null;
    const hunk = picked ? picked.closest(".hunk") : currentHunk();
    setCurrent(hunk); // the outline shows which hunk the buttons act on
    const { comment, mark } = bar.parts;
    // Comment needs write access (lines then carry a comment action); marks exist only in feature scope.
    comment.hidden = !document.querySelector("#app .ln-btn");
    comment.disabled = !hunk || !hunk.querySelector(".ln-btn");
    comment.textContent = picked ? `Comment on line ${picked.dataset.line}` : "Comment";
    const markBtn = hunk && hunk.querySelector(".mark-btn");
    mark.hidden = !document.querySelector("#app .mark-btn");
    mark.disabled = !markBtn;
    mark.setAttribute("aria-pressed", markBtn ? markBtn.getAttribute("aria-pressed") : "false");
  }

  let frame = 0;
  const schedule = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; sync(); }); };
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  if (touchQuery && touchQuery.addEventListener) touchQuery.addEventListener("change", schedule);
  // Pages render, and Story mode loads each commit's hunks, after navigation: watch the app for them.
  new MutationObserver(schedule).observe(document.getElementById("app") || document.body, { childList: true, subtree: true });
  window.addEventListener("hashchange", () => { picked = null; });

  // Tap a code line to pick it for a comment; tap it again to let go.
  document.addEventListener("click", (evt) => {
    if (!touch() || !evt.target.closest) return;
    const tr = evt.target.closest("table.code tr");
    if (!tr || !tr._comment || evt.target.closest("a, button, input, textarea, select")) return;
    const again = picked === tr;
    unpick();
    if (!again) {
      picked = tr;
      tr.classList.add("picked");
      setCurrent(tr.closest(".hunk"));
    }
    sync();
  });

  // The on-screen keyboard needs the room while a comment is being written.
  const typing = (el) => el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && /^(text|search|password)$/.test(el.type));
  document.addEventListener("focusin", (evt) => { if (bar) bar.classList.toggle("typing", typing(evt.target)); });
  document.addEventListener("focusout", () => { if (bar) bar.classList.remove("typing"); });

  // --- new evidence notice (serve --watch) -----------------------------------------------------------
  async function poll() {
    if (readonly() || document.visibilityState !== "visible") return;
    try {
      const resp = await fetch("/api/v1/meta", { credentials: "same-origin" });
      if (!resp.ok) return;
      const meta = await resp.json();
      // Our own writes report the generation they produced (core.js keeps it), so they don't count as news.
      if (D.knownGeneration === undefined || D.knownGeneration === null) { D.knownGeneration = meta.generation; return; }
      if (meta.generation !== D.knownGeneration) {
        D.knownGeneration = meta.generation;
        if (document.querySelector(".notice")) return;
        const note = h("div", { class: "notice", role: "status" }, "Records or evidence changed. ",
          h("button", { class: "btn small", type: "button", onclick: () => { note.remove(); D.invalidate(); D.rerender(); } }, "Show the latest"),
          h("button", { class: "btn small", type: "button", onclick: () => note.remove() }, "Dismiss"));
        document.body.appendChild(note);
      }
    } catch (err) { /* offline or restarting; try again later */ }
  }
  if (!readonly()) {
    setTimeout(poll, 500);
    setInterval(poll, 15000);
  }
})();
