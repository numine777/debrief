"""Commit lookup by SHA, squash-commit mapping, and ``debrief squash``.

Providers drop the original messages from squash commits and the repository
holds nothing from Debrief, so a squash commit is matched to its feature by
content (decision D4):

1. tree match: the commit's tree equals a feature's archived head tree;
2. patch match: its patch-id equals the feature's cumulative patch-id;
3. per-file match: the share of its changed files whose new blob equals the
   feature's head version, reported with that share as the confidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config, diffparse, gitutil, index, paths, projects, records, util


def _features_with_evidence(project_id: str, root: Optional[Path] = None):
    base = paths.project_dir(project_id, root) / "features"
    if not base.is_dir():
        return
    for feature_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        evidence = util.read_json(feature_dir / "evidence" / "evidence.json", None)
        if isinstance(evidence, dict):
            yield feature_dir.name, feature_dir, evidence


def match_commit(repo: Path, sha: str, project_id: str, root: Optional[Path] = None) -> List[dict]:
    """Rank features that a (squash) commit could have landed, best first."""
    tree = gitutil.tree_of(repo, sha)
    meta = gitutil.commit_meta(repo, sha) or {}
    parents = meta.get("parents") or []
    patch = gitutil.run(["diff", *gitutil.DIFF_ARGS, parents[0], sha], repo) if parents else gitutil.commit_patch(repo, sha)
    pid = gitutil.patch_id(repo, patch)
    files = diffparse.parse(patch)
    matches = []
    for feature_id, feature_dir, evidence in _features_with_evidence(project_id, root):
        trees = {evidence.get("head_tree")}
        for leg in records.load_legs(feature_dir):
            trees.add(leg.get("head_tree"))
        trees.discard(None)
        if tree and tree in trees:
            matches.append({"feature_id": feature_id, "method": "tree", "confidence": 1.0,
                            "detail": "commit tree equals the feature's archived head tree"})
            continue
        if pid and pid == evidence.get("patch_id"):
            matches.append({"feature_id": feature_id, "method": "patch-id", "confidence": 1.0,
                            "detail": "commit patch-id equals the feature's cumulative patch-id"})
            continue
        head_blobs = {f["path"]: f.get("new_blob") for f in evidence.get("files", [])}
        if not files or not head_blobs:
            continue
        same = sum(1 for f in files if f.path in head_blobs and f.new_blob and f.new_blob == head_blobs[f.path])
        overlap = sum(1 for f in files if f.path in head_blobs)
        if same == 0:
            continue
        confidence = round(same / max(len(files), len(head_blobs)), 3)
        matches.append({"feature_id": feature_id, "method": "files", "confidence": confidence,
                        "detail": f"{same} of {len(files)} changed files match the feature's head versions "
                                  f"({overlap} paths in common)"})
    matches.sort(key=lambda m: -m["confidence"])
    return matches


def record_landing(project_id: str, feature_id: str, sha: str, match: dict, root: Optional[Path] = None) -> None:
    path = paths.feature_dir(project_id, feature_id, root) / "evidence" / "landed.json"
    data = util.read_json(path, None) or {"commits": []}
    data["commits"] = [c for c in data.get("commits", []) if c.get("sha") != sha]
    data["commits"].append({"sha": sha, "method": match["method"], "confidence": match["confidence"],
                            "detail": match["detail"], "landed_at": util.now_iso()})
    util.write_json(path, data)
    index.set_landed(project_id, feature_id, sha, match["method"], match["confidence"], match["detail"], root)


def follow_default_branch(project_id: str, root: Optional[Path] = None, limit: int = 200) -> List[Tuple[str, dict]]:
    """Map new commits on the default branch to features. Returns the landings recorded."""
    repo = projects.repo_workdir(project_id, root)
    if repo is None:
        return []
    ref = gitutil.default_branch_ref(repo)
    if not ref:
        return []
    tip = gitutil.rev_parse(repo, ref)
    conn = index.connect(root)
    try:
        key = f"default_tip:{project_id}"
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        last = row["value"] if row else None
        if last == tip:
            return []
        if last and gitutil.is_ancestor(repo, last, tip):
            new = gitutil.rev_list(repo, f"{last}..{tip}", first_parent=True)
        else:
            new = gitutil.rev_list(repo, f"{tip}~{limit}..{tip}", first_parent=True) or [tip]
        known = {r["sha"] for r in conn.execute("SELECT sha FROM commits WHERE project_id=?", (project_id,))}
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, tip))
        conn.commit()
    finally:
        conn.close()
    landed = []
    for sha in new[-limit:]:
        if sha in known:
            continue
        best = match_commit(repo, sha, project_id, root)
        if best and best[0]["confidence"] >= 0.5:
            record_landing(project_id, best[0]["feature_id"], sha, best[0], root)
            landed.append((sha, best[0]))
    return landed


def describe(sha: str, repo: Optional[Path] = None, root: Optional[Path] = None) -> dict:
    """Everything Debrief knows about a commit."""
    resolved = gitutil.rev_parse(repo, f"{sha}^{{commit}}") if repo is not None else None
    full = resolved or sha
    rows = index.lookup_commit(full, root)
    result: Dict[str, object] = {"sha": full, "features": [], "landed": []}
    for row in rows:
        target = result["landed"] if row["match"] == "landed" else result["features"]
        commit = util.read_json(paths.feature_dir(row["project_id"], row["feature_id"], root) / "evidence" / "commits"
                                / f"{row['sha']}.json", {}) or {}
        target.append(dict(row, systems=commit.get("systems", []), session_id=commit.get("session_id"),
                           subject=commit.get("subject") or row.get("subject"), body=commit.get("body", "")))
    # Only a commit on the default branch can have landed a feature: an amended-away commit or a
    # throwaway branch with the same tree is not a landing, and recording one would mislabel it.
    if not rows and resolved and _on_default_branch(repo, resolved):
        found = projects.find_project(repo)
        if found:
            for match in match_commit(repo, full, found[0], root):
                if match["confidence"] >= 0.5:
                    record_landing(found[0], match["feature_id"], full, match, root)
                    result["landed"].append(dict(match, project_id=found[0], sha=full, match="landed"))
    return result


def _on_default_branch(repo: Path, sha: str) -> bool:
    return any(gitutil.is_ancestor(repo, sha, ref) for ref in gitutil.upstream_refs(repo, None))


def cli_show(args) -> int:
    repo = Path(args.repo) if args.repo else Path.cwd()
    repo = gitutil.toplevel(repo) or (repo if gitutil.common_dir(repo) else None)
    info = describe(args.sha, repo)
    port = config.load().port
    sha = info["sha"]
    if not info["features"] and not info["landed"]:
        print(f"{sha}: not in any Debrief feature" + ("" if repo else " (run inside the repo to match squash commits)"))
        return 1
    for row in info["features"]:
        kind = {"agent": "agent commit", "developer": "developer commit", "merge": "merge"}.get(row["kind"], row["kind"])
        print(f"{row['sha'][:12]}  {row.get('subject') or ''}")
        print(f"  feature  {row['project_id']}/{row['feature_id']}  {row.get('leg_id') or 'no leg yet'}  ({kind}"
              + (f", session {row['session_id']}" if row.get("session_id") else "") + ")")
        if row.get("systems"):
            print(f"  systems  {', '.join(row['systems'])}")
        print(f"  viewer   http://127.0.0.1:{port}/#/commit/{row['sha']}")
    for row in info["landed"]:
        print(f"{row['sha'][:12]}  landed {row['project_id']}/{row['feature_id']} by {row['method']} match "
              f"({row['confidence'] * 100:.0f}%): {row.get('detail', '')}")
        print(f"  viewer   http://127.0.0.1:{port}/#/p/{row['project_id']}/f/{row['feature_id']}")
    return 0


# --- debrief squash ------------------------------------------------------------------------------


def squash_feature(project_id: str, feature_id: str, root: Optional[Path] = None) -> Path:
    """Write the deterministic consolidated record: squash/record.md and squash/index.json."""
    feature_dir = paths.feature_dir(project_id, feature_id, root)
    feature = records.load_feature(feature_dir)
    evidence = util.read_json(feature_dir / "evidence" / "evidence.json", {}) or {}
    out_dir = feature_dir / "squash"
    legs = []
    evidence_legs = {leg["leg_id"]: leg for leg in evidence.get("legs", [])}
    for leg in feature["legs"]:
        ev = evidence_legs.get(leg["leg_id"], {})
        handoffs = []
        for sess in feature["sessions"]:
            if sess["meta"].get("leg_id") == leg["leg_id"]:
                handoffs += [{"session_id": sess["meta"].get("session_id"), "at": e["at"], "text": e["text"]}
                             for e in sess["journal"] if e["kind"] == "handoff"]
        legs.append({
            "leg_id": leg["leg_id"], "base": leg.get("base_ref"), "head": leg.get("head_commit") or leg.get("head_ref"),
            "closed_at": leg.get("closed_at"), "sessions": [s.get("session_id") if isinstance(s, dict) else s
                                                            for s in leg.get("sessions", [])],
            "commits": ev.get("commits") or [c.get("sha") for c in leg.get("commits", [])],
            "handoffs": handoffs,
        })
    landed = util.read_json(feature_dir / "evidence" / "landed.json", {}) or {}
    index_data = {
        "squashed_at": util.now_iso(), "project_id": project_id, "feature_id": feature_id,
        "base": evidence.get("effective_base") or evidence.get("base"), "head": evidence.get("head_commit"),
        "landed": landed.get("commits", []), "legs": legs,
        "systems": [s["meta"]["id"] for s in feature["systems"]],
        "decisions": [d["meta"]["id"] for d in feature["decisions"]],
        "tests": [t["id"] for t in (feature.get("tests") or {}).get("tests", [])],
    }
    util.write_json(out_dir / "index.json", index_data)
    brief = feature.get("brief") or {}
    title = brief.get("meta", {}).get("title") or feature_id
    lines = [
        "---", f"feature_id: {feature_id}", f"title: {json.dumps(title)}", "kind: consolidated-record",
        f"squashed_at: {index_data['squashed_at']}", "model_written: false", "---", "",
        f"# {title}", "",
        "Consolidated record of every leg, written deterministically from the agent's final records.", "",
    ]
    if brief:
        lines += [brief["body"].strip(), ""]
    lines += ["## Systems", ""]
    for system in feature["systems"]:
        meta = system["meta"]
        change = records.find_section(system["sections"], "Change") or ""
        lines.append(f"- **{meta.get('title') or meta['id']}** (`{meta['id']}`, {meta.get('change')}): "
                     + " ".join(change.split()))
    lines += ["", "## Tests", ""]
    for test in (feature.get("tests") or {}).get("tests", []):
        lines.append(f"- `{test['id']}`: {test['claim']} (`{test['command']}`)")
    for gap in (feature.get("tests") or {}).get("gaps", []):
        lines.append(f"- Gap ({gap['validates'] or 'general'}): {gap['note']}")
    lines += ["", "## Decisions", ""]
    for decision in feature["decisions"]:
        lines.append(f"- `{decision['meta']['id']}`: {decision['meta'].get('title')}")
    lines += ["", "## Legs", ""]
    for leg in legs:
        lines.append(f"### {leg['leg_id']}" + (f" (closed {leg['closed_at']})" if leg["closed_at"] else " (open)"))
        lines.append("")
        lines.append(f"Commits: {', '.join(c[:9] for c in leg['commits']) or 'none'}")
        for handoff in leg["handoffs"]:
            lines.append("")
            lines.append(f"Handoff {handoff['at']}: " + " ".join(handoff["text"].split()))
        lines.append("")
    if landed.get("commits"):
        lines += ["## Landed as", ""]
        for item in landed["commits"]:
            lines.append(f"- `{item['sha'][:12]}` by {item['method']} match ({item['confidence'] * 100:.0f}%)")
    util.write_text(out_dir / "record.md", "\n".join(lines).rstrip() + "\n")
    return out_dir


def cli_squash(args) -> int:
    project_id = args.project
    if not project_id:
        found = projects.find_project(Path.cwd())
        if not found:
            matches = [pid for pid in projects.list_projects() if (paths.feature_dir(pid, args.feature)).exists()]
            if len(matches) != 1:
                print("Pass --project: the feature id is ambiguous or unknown here.")
                return 1
            project_id = matches[0]
        else:
            project_id = found[0]
    feature_id = util.feature_id_for_branch(args.feature) if "/" in args.feature else args.feature
    if not paths.feature_dir(project_id, feature_id).exists():
        print(f"No feature {feature_id} in project {project_id}.")
        return 1
    out = squash_feature(project_id, feature_id)
    print(f"Wrote {out / 'record.md'} and {out / 'index.json'}.")
    if getattr(args, "model", False):
        from . import compaction

        try:
            path = compaction.compact_feature(project_id, feature_id)
        except compaction.CompactionError as exc:
            print(f"Model compaction skipped: {exc}")
            return 1
        print(f"Wrote model-written compaction to {path} (experimental; labeled as model-written).")
    return 0
