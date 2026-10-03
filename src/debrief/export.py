"""``debrief export``: one self-contained HTML file for a feature.

The file embeds the viewer, its fonts and the feature's data (the same API
responses the server would give), so a teammate can read the feature from a PR
attachment without Debrief. Its content security policy allows no network
requests at all: inline scripts and styles are permitted only by hash, fonts
and images only as data URIs. Only shared comments are included; private
comments and review marks stay on the host.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

from . import __version__, comments, paths, projects, resources, util
from .api import Api, BadRequest, NotFound

SCRIPTS = ["static/vendor/marked.min.js", "static/vendor/purify.min.js", "static/vendor/highlight.min.js",
           "static/js/core.js", "static/js/diff.js", "static/js/views.js", "static/js/review.js", "static/js/app.js"]
MAX_BLOB_TEXT = 6 * 1024 * 1024


def _enc(value: str) -> str:
    """Match the front end's encodeURIComponent for the ids and scopes Debrief uses."""
    return quote(value, safe="-_.!~*'()")


def _sha256(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _inline_fonts(css: str) -> str:
    out = css
    for name in resources.list_files("static/fonts"):
        if not name.endswith(".woff2"):
            continue
        data = base64.b64encode(resources.read_bytes(name)).decode("ascii")
        rel = name[len("static/"):]
        out = out.replace(f'url("{rel}")', f'url("data:font/woff2;base64,{data}")')
    return out


_HTML_SENSITIVE = re.compile(r"<(!--|/?script)", re.IGNORECASE)


def script_safe(text: str) -> str:
    """Escape sequences that would end or re-scope an inline script element.

    ``<!--`` and ``<script`` switch the HTML parser into escaped states where
    the closing tag stops counting. Writing the ``<`` as ``\\x3C`` keeps the same
    meaning inside JavaScript strings and regular expressions, where the
    vendored libraries use them.
    """
    return _HTML_SENSITIVE.sub(lambda m: "\\x3C" + m.group(1), text)


def collect(api: Api, pid: str, fid: str) -> dict:
    """Every API response the viewer needs for one feature, keyed by path.

    An export carries this feature's records and nothing else: commit and epic
    pages are limited to it, so a file attached to a PR never includes another
    feature or project that shares a commit or an epic name (on the hub, one
    the exporter may not even be allowed to see).
    """
    def only_this(project_id: str, feature_id: str) -> bool:
        return project_id == pid and feature_id == fid

    feature = api.feature(pid, fid)
    feature_dir = api.feature_dir(pid, fid)
    evidence = util.read_json(feature_dir / "evidence" / "evidence.json", {}) or {}
    shared = [c for c in comments.load_all(pid, fid, api.root) if c.get("visibility") == "shared"]
    feature["comments"] = comments.locate(shared, feature_dir, evidence)
    feature["marks"] = []
    feature["review"] = None
    base = f"/projects/{_enc(pid)}/features/{_enc(fid)}"
    routes: Dict[str, object] = {}
    meta = api.meta()
    meta.update(mode="export", readonly=True, archive=None, exported_at=util.now_iso())
    routes["/meta"] = meta
    index = api.index_view()
    for project in index["projects"]:
        project["features"] = [f for f in project["features"] if only_this(f["project_id"], f["feature_id"])]
    index["projects"] = [p for p in index["projects"] if p["features"]]
    index["epics"] = {f["epic"]: 1 for p in index["projects"] for f in p["features"] if f.get("epic")}
    routes["/index"] = index
    routes[base] = feature
    routes[f"{base}/diff?scope=feature"] = api.diff(pid, fid, "feature")
    blobs: Dict[str, str] = {}
    budget = MAX_BLOB_TEXT
    for f in routes[f"{base}/diff?scope=feature"]["files"]:
        for blob in (f.get("old_blob"), f.get("new_blob")):
            if not blob or blob in blobs or f.get("binary"):
                continue
            try:
                text = api.blob(pid, fid, blob).decode("utf-8", "replace")
            except NotFound:
                continue
            if len(text) > budget:
                continue
            budget -= len(text)
            blobs[blob] = text
    for commit in evidence.get("commits") or []:
        sha = commit["sha"]
        try:
            routes[f"{base}/diff?scope={_enc('commit:' + sha)}"] = api.diff(pid, fid, "commit:" + sha)
        except NotFound:
            continue
        try:
            routes[f"/commits/{sha}"] = api.commit(sha, allow=only_this)
        except (NotFound, BadRequest):  # a commit page is optional in an export
            pass
    if feature.get("epic"):
        try:
            routes[f"/epics/{_enc(feature['epic'])}"] = api.epic(feature["epic"], allow=only_this)
        except NotFound:
            pass
    return {"routes": routes, "blobs": blobs, "start": f"#/p/{_enc(pid)}/f/{_enc(fid)}",
            "exported_at": meta["exported_at"], "version": __version__}


def render(api: Api, pid: str, fid: str) -> str:
    payload = collect(api, pid, fid)
    # Every "<" in the data becomes \u003c, so code in diffs can never close or
    # re-open the script element; line and paragraph separators are escaped too.
    data_js = "window.DEBRIEF_EXPORT = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) \
        .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029") + ";"
    scripts = [data_js] + [script_safe(resources.read_text(name)) for name in SCRIPTS]
    css = _inline_fonts(resources.read_text("static/app.css"))
    favicon = base64.b64encode(resources.read_bytes("static/favicon.svg")).decode("ascii")
    csp = ("default-src 'none'; script-src " + " ".join(_sha256(s) for s in scripts) +
           "; style-src " + _sha256(css) + "; font-src data:; img-src data:; base-uri 'none'; form-action 'none'")
    title = payload["routes"][f"/projects/{_enc(pid)}/features/{_enc(fid)}"]["title"]
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>{_html_escape(title)} · Debrief export</title>",
        f'<link rel="icon" href="data:image/svg+xml;base64,{favicon}">',
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        '<div id="app"><p class="loading">Loading…</p></div>',
    ]
    for script in scripts:
        parts.append(f"<script>{script}</script>")
    parts += ["</body>", "</html>", ""]
    return "\n".join(parts)


def _html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def cli(args) -> int:
    api = Api(paths.archive_root(), mode="local")
    pid = args.project
    fid = args.feature
    if "/" in fid:
        fid = util.feature_id_for_branch(fid)
    if not pid:
        found = projects.find_project(Path.cwd())
        candidates = [found[0]] if found else [p for p in projects.list_projects() if paths.feature_dir(p, fid).exists()]
        if len(candidates) != 1:
            print("Pass --project: the feature id is ambiguous or unknown here.")
            return 1
        pid = candidates[0]
    try:
        html = render(api, pid, fid)
    except NotFound as exc:
        print(f"debrief export: {exc}")
        return 1
    out = Path(args.output or f"debrief-{fid}.html")
    util.write_text(out, html)
    size = out.stat().st_size
    print(f"Wrote {out} ({size / 1024:.0f} KiB). It contains code excerpts: share it only where your data policy allows.")
    return 0
