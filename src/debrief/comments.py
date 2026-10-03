"""Line comments, review marks, prompts and feedback (decision D7).

Comments are the reviewer's notes first and feedback to the agent second.

* Shared comments live in the feature's ``comments.json`` in the archive and
  sync with it; private ones live in ``<archive>/.viewer/`` and never leave
  the host.
* Each comment records the commit, path, side, line, the line's text and the
  file version (blob) it was made on. When later commits move the line, the
  comment follows it; when they change or delete it, it is marked outdated
  and keeps its original context.
* Review marks key on hunk content hashes, so a mark clears by itself when the
  agent changes that code.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import diffparse, paths, util

STATES = ("open", "sent", "resolved")
VISIBILITY = ("private", "shared")
MAX_BODY_CHARS = 20000
MAX_LINE = 10_000_000


class CommentError(ValueError):
    pass


# --- storage ------------------------------------------------------------------------


def shared_path(project_id: str, feature_id: str, root: Optional[Path] = None) -> Path:
    return paths.feature_dir(project_id, feature_id, root) / "comments.json"


def private_path(project_id: str, feature_id: str, root: Optional[Path] = None) -> Path:
    return paths.viewer_state_dir(root) / project_id / feature_id / "comments.json"


def _lock(project_id: str, feature_id: str, root: Optional[Path]):
    return util.file_lock(paths.viewer_state_dir(root) / "comments.lock")


def _read(path: Path) -> List[dict]:
    data = util.read_json(path, None)
    if isinstance(data, dict) and isinstance(data.get("comments"), list):
        return [c for c in data["comments"] if isinstance(c, dict) and c.get("id")]
    return []


def _write(path: Path, comments: List[dict]) -> None:
    util.write_json(path, {"comments": sorted(comments, key=lambda c: c.get("created_at") or "")})


def load_all(project_id: str, feature_id: str, root: Optional[Path] = None, user: Optional[str] = None) -> List[dict]:
    shared = [dict(c, visibility="shared") for c in _read(shared_path(project_id, feature_id, root))]
    private = [dict(c, visibility="private") for c in _read(private_path(project_id, feature_id, root))]
    if user is not None:
        private = [c for c in private if c.get("author") in (None, user)]
    return sorted(shared + private, key=lambda c: c.get("created_at") or "")


def _find(project_id: str, feature_id: str, root: Optional[Path], comment_id: str,
          user: str) -> Tuple[Path, List[dict], dict]:
    """A shared comment, or one of ``user``'s private ones. Others' private comments don't exist for them."""
    for path in (shared_path(project_id, feature_id, root), private_path(project_id, feature_id, root)):
        items = _read(path)
        shared = path == shared_path(project_id, feature_id, root)
        for item in items:
            if item.get("id") == comment_id and (shared or item.get("author") in (None, user)):
                return path, items, item
    raise KeyError(comment_id)


def _line_number(value) -> int:
    if isinstance(value, bool):
        raise CommentError("anchor.line must be a line number")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not 1 <= value <= MAX_LINE:
        raise CommentError("anchor.line must be a line number")
    return value


def _text(value, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CommentError(f"{what} must be text")
    return value


def create(project_id: str, feature_id: str, data: dict, author: str, root: Optional[Path] = None) -> dict:
    anchor = data.get("anchor") or {}
    if not isinstance(anchor, dict):
        raise CommentError("anchor must be an object with path, side and line")
    body = _text(data.get("body"), "body").strip()
    if not body:
        raise CommentError("a comment needs some text")
    if len(body) > MAX_BODY_CHARS:
        raise CommentError("that comment is too long")
    side = anchor.get("side")
    if side not in ("new", "old"):
        raise CommentError("anchor.side must be new or old")
    line = _line_number(anchor.get("line"))
    if not anchor.get("path") or not isinstance(anchor.get("path"), str):
        raise CommentError("anchor.path is required")
    visibility = data.get("visibility") or "private"
    if visibility not in VISIBILITY:
        raise CommentError("visibility must be private or shared")
    now = util.now_iso()
    comment = {
        "id": "c-" + secrets.token_hex(6),
        "created_at": now,
        "updated_at": now,
        "author": author,
        "state": "open",
        "anchor": {
            "commit": str(anchor.get("commit") or ""),
            "scope": str(anchor.get("scope") or "feature"),
            "path": str(anchor["path"]),
            "side": side,
            "line": line,
            "text": str(anchor.get("text") or "")[:500],
            "blob": str(anchor.get("blob") or "") or None,
        },
        "body": body,
        "sent_at": None,
        "resolved_at": None,
        "replies": [],
    }
    path = shared_path(project_id, feature_id, root) if visibility == "shared" else private_path(project_id, feature_id, root)
    with _lock(project_id, feature_id, root):
        items = _read(path)
        items.append(comment)
        _write(path, items)
    return dict(comment, visibility=visibility)


def update(project_id: str, feature_id: str, comment_id: str, data: dict, user: str,
           root: Optional[Path] = None) -> Tuple[dict, bool]:
    """Edit body, state or visibility, or add a reply. Returns (comment, shared_changed).

    Only the author edits the text or changes visibility: unsharing someone
    else's comment would hide it from the team and the agent. On a shared
    comment, anyone who may write review records can reply and change its
    state, as in a pull request conversation; who did it is recorded.
    """
    with _lock(project_id, feature_id, root):
        try:
            path, items, item = _find(project_id, feature_id, root, comment_id, user)
        except KeyError:
            raise CommentError("no such comment") from None
        shared_before = path == shared_path(project_id, feature_id, root)
        is_author = item.get("author") in (None, user)
        target_visibility = data.get("visibility")
        if target_visibility is not None and target_visibility not in VISIBILITY:
            raise CommentError("visibility must be private or shared")
        moving = target_visibility is not None and (target_visibility == "shared") != shared_before
        if moving and not is_author:
            raise PermissionError("only the author can share or unshare a comment")
        if "body" in data and not is_author:
            raise PermissionError("only the author can edit a comment")
        now = util.now_iso()
        if "body" in data:
            body = _text(data["body"], "body").strip()
            if not body:
                raise CommentError("a comment needs some text")
            item["body"] = body[:MAX_BODY_CHARS]
        if "state" in data:
            state = data["state"]
            if state not in STATES:
                raise CommentError("state must be open, sent or resolved")
            if state != item.get("state"):
                item["state"] = state
                item["state_changed_by"] = user
            item["resolved_at"] = now if state == "resolved" else None
            item["resolved_by"] = user if state == "resolved" else None
            if state == "sent" and not item.get("sent_at"):
                item["sent_at"] = now
        if "reply" in data:
            text = _text(data["reply"], "reply").strip()
            if text:
                item.setdefault("replies", []).append({"id": "r-" + secrets.token_hex(4), "author": user,
                                                      "body": text[:MAX_BODY_CHARS], "created_at": now})
        item["updated_at"] = now
        if moving:
            items = [c for c in items if c.get("id") != comment_id]
            _write(path, items)
            other = shared_path(project_id, feature_id, root) if target_visibility == "shared" else private_path(
                project_id, feature_id, root)
            others = _read(other)
            others.append(item)
            _write(other, others)
            return dict(item, visibility=target_visibility), True
        _write(path, items)
        return dict(item, visibility="shared" if shared_before else "private"), shared_before


def delete(project_id: str, feature_id: str, comment_id: str, user: str, root: Optional[Path] = None) -> bool:
    """Delete a comment. Returns whether it was shared."""
    with _lock(project_id, feature_id, root):
        try:
            path, items, item = _find(project_id, feature_id, root, comment_id, user)
        except KeyError:
            raise CommentError("no such comment") from None
        if item.get("author") not in (None, user):
            raise PermissionError("only the author can delete a comment")
        _write(path, [c for c in items if c.get("id") != comment_id])
        return path == shared_path(project_id, feature_id, root)


# --- where a comment is now -----------------------------------------------------------


def _blob_text(feature_dir: Path, blob: Optional[str]) -> Optional[str]:
    if not blob:
        return None
    path = feature_dir / "evidence" / "blobs" / blob
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def locate(comments: List[dict], feature_dir: Path, evidence: dict) -> List[dict]:
    """Add ``current`` to each comment: its line at the feature head, or outdated."""
    files = {f["path"]: f for f in evidence.get("files") or []}
    head = evidence.get("head")
    cache: Dict[Tuple[str, str], Dict[int, int]] = {}
    out = []
    for comment in comments:
        anchor = comment.get("anchor") or {}
        path = anchor.get("path")
        line = anchor.get("line")
        current = {"line": None, "outdated": True, "moved": False, "head": head}
        f = files.get(path)
        if anchor.get("side") == "old":
            if f and (not anchor.get("blob") or anchor.get("blob") == f.get("old_blob")):
                current.update(line=line, outdated=False)
        elif f and f.get("new_blob"):
            now_blob = f["new_blob"]
            if anchor.get("blob") in (None, "", now_blob):
                current.update(line=line, outdated=False)
            else:
                key = (anchor["blob"], now_blob)
                if key not in cache:
                    old_text = _blob_text(feature_dir, anchor["blob"])
                    new_text = _blob_text(feature_dir, now_blob)
                    cache[key] = diffparse.line_map(old_text, new_text) if old_text is not None and new_text is not None else {}
                mapped = cache[key].get(line)
                if mapped is not None:
                    new_text = _blob_text(feature_dir, now_blob) or ""
                    lines = new_text.splitlines()
                    text_now = lines[mapped - 1] if 0 < mapped <= len(lines) else None
                    if text_now is not None and text_now.strip() == (anchor.get("text") or "").strip():
                        current.update(line=mapped, outdated=False, moved=mapped != line)
        out.append(dict(comment, current=current))
    return out


# --- prompts and feedback -------------------------------------------------------------------


def build_prompt(comments: List[dict], feature: dict, head: Optional[str]) -> str:
    """The copy-ready prompt for the agent harness, one numbered item per comment."""
    branch = feature.get("branch") or feature.get("feature_id")
    head_text = f" (head {head[:7]})" if head and head != "WORKTREE" else (" (uncommitted head)" if head == "WORKTREE" else "")
    lines = [
        f"Review comments on {branch}{head_text}. Address each one, log what",
        "you change in your journal, and commit each fix atomically.",
    ]
    for number, comment in enumerate(comments, 1):
        anchor = comment.get("anchor") or {}
        current = comment.get("current") or {}
        path = anchor.get("path")
        reviewed_at = (anchor.get("commit") or "")[:7] or "an earlier head"
        if anchor.get("side") == "old":
            where = f"{path} (removed line {anchor.get('line')}, as reviewed at {reviewed_at})"
        elif current.get("outdated"):
            where = f"{path} (was line {anchor.get('line')} when reviewed at {reviewed_at}; that line has since changed)"
        elif current.get("moved"):
            where = f"{path}:{current.get('line')} (was line {anchor.get('line')} when reviewed at {reviewed_at})"
        else:
            where = f"{path}:{current.get('line') or anchor.get('line')}"
        lines.append("")
        lines.append(f"{number}. {where}")
        text = (anchor.get("text") or "").strip()
        if text:
            lines.append(f"   > {text}")
        for body_line in str(comment.get("body") or "").strip().splitlines():
            lines.append(f"   {body_line}" if body_line.strip() else "")
    return "\n".join(lines).rstrip() + "\n"


def queue_feedback(project_id: str, feature_id: str, prompt: str, author: str, root: Optional[Path] = None) -> Path:
    """Append the prompt to feedback.md, which bin/session now relays to the agent."""
    path = paths.feature_dir(project_id, feature_id, root) / "feedback.md"
    with util.file_lock(path.with_name(".feedback.lock")):
        existing = path.read_text(encoding="utf-8") if path.exists() else (
            "# Review feedback\n\nWritten by the Debrief viewer. Each section is one round of review comments.\n")
        section = f"\n## {util.now_iso()} · {author}\n\n{prompt.rstrip()}\n"
        util.write_text(path, existing.rstrip("\n") + "\n" + section)
    return path


def mark_sent(project_id: str, feature_id: str, ids: List[str], user: str, root: Optional[Path] = None) -> bool:
    """Mark comments sent. Returns whether any shared comment changed."""
    shared_changed = False
    for cid in ids:
        try:
            comment, shared = update(project_id, feature_id, cid, {"state": "sent"}, user, root)
        except CommentError:
            continue
        shared_changed = shared_changed or shared
    return shared_changed


# --- review marks --------------------------------------------------------------------------


def _marks_path(root: Optional[Path]) -> Path:
    return paths.viewer_state_dir(root) / "marks.json"


def marks(project_id: str, feature_id: str, user: str, root: Optional[Path] = None) -> Dict[str, str]:
    data = util.read_json(_marks_path(root), {}) or {}
    return dict(((data.get(f"{project_id}/{feature_id}") or {}).get(user) or {}))


def set_mark(project_id: str, feature_id: str, user: str, hunk_id: str, reviewed: bool,
             root: Optional[Path] = None) -> Dict[str, str]:
    if not hunk_id or len(hunk_id) > 80:
        raise CommentError("invalid hunk id")
    path = _marks_path(root)
    with util.file_lock(path.with_name("marks.lock")):
        data = util.read_json(path, {}) or {}
        feature_marks = data.setdefault(f"{project_id}/{feature_id}", {})
        mine = feature_marks.setdefault(user, {})
        if reviewed:
            mine[hunk_id] = util.now_iso()
        else:
            mine.pop(hunk_id, None)
        util.write_json(path, data)
        return dict(mine)


def review_progress(project_id: str, feature_id: str, user: str, hunk_ids: List[str], root: Optional[Path] = None) -> dict:
    mine = marks(project_id, feature_id, user, root)
    current = set(hunk_ids)
    done = [h for h in mine if h in current]
    return {"reviewed": len(done), "total": len(current)}
