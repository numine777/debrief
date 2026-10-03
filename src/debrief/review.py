"""The review loop on the server: comments, prompts, marks, Close leg, export.

Writes that other hosts must see (shared comments, queued feedback, close
requests) are committed to the project's archive repository and pushed in the
background when it has a remote, so ``bin/session now`` relays them on the
agent's host. Private comments and review marks never leave this host.
"""

from __future__ import annotations

import getpass
import threading
from pathlib import Path
from typing import List, Optional

from . import archive, comments, paths, projects, records, session, util
from .api import Api, BadRequest, NotFound
from .server import App, Request, Response


def local_user() -> str:
    try:
        return getpass.getuser() or "developer"
    except Exception:  # some sandboxes have no passwd entry
        return "developer"


def close_prompt(feature_id: str, branch: Optional[str], leg_id: str) -> str:
    skill = session.skills_dir() / session.CLOSEOUT_SKILL
    try:
        skill_text = "~/" + str(skill.relative_to(Path.home()))
    except ValueError:
        skill_text = str(skill)
    return (f"Close out {leg_id} of {branch or feature_id} now: follow {skill_text} "
            "and complete every step, ending with `bin/session publish`.")


def request_close(project_id: str, feature_id: str, user: str, note: Optional[str] = None,
                  root: Optional[Path] = None) -> dict:
    """Record a developer's close request on the open leg. Returns the leg and a prompt."""
    feature_dir = paths.feature_dir(project_id, feature_id, root)
    with util.file_lock(feature_dir / ".feature.lock"):
        legs = records.load_legs(feature_dir)
        if not legs or legs[-1].get("closed_at"):
            raise BadRequest("there is no open leg to close; the next session opens one")
        leg = legs[-1]
        if not leg.get("close_requested_at"):
            leg["close_requested_at"] = util.now_iso()
        leg["close_requested_by"] = user
        if note:
            leg["close_note"] = str(note)[:500]
        util.write_json(feature_dir / "legs" / f"{leg['leg_id']}.json", leg)
    return {"leg_id": leg["leg_id"], "close_requested_at": leg["close_requested_at"],
            "prompt": close_prompt(feature_id, leg.get("branch"), leg["leg_id"])}


def publish_records(project_id: str, message: str, root: Optional[Path] = None, wait: bool = False) -> None:
    """Commit the project's archive and, if it has a remote, sync it in the background."""
    project_dir = paths.project_dir(project_id, root)
    if not archive.is_repo(project_dir):
        return
    with archive.lock(project_dir):
        archive.commit(project_dir, f"{message} from {util.hostname()}")
    if not archive.remote_url(project_dir):
        return

    def sync() -> None:
        try:
            archive.sync(project_dir, message)
        except Exception:  # the next sync retries; the viewer must not fail
            pass

    if wait:
        sync()
    else:
        threading.Thread(target=sync, name=f"sync-{project_id}", daemon=True).start()


class ReviewApp(App):
    """The local viewer with the review loop."""

    def __init__(self, api: Api, port: int, allowed_hosts: Optional[List[str]] = None):
        self.generation = 0
        super().__init__(api, port, allowed_hosts)

    def extend_routes(self, F: str) -> None:
        self.add("GET", F + r"/comments", self.get_comments)
        self.add("POST", F + r"/comments", self.post_comment)
        self.add("PATCH", F + r"/comments/(?P<cid>c-[0-9a-f]+)", self.patch_comment)
        self.add("DELETE", F + r"/comments/(?P<cid>c-[0-9a-f]+)", self.delete_comment)
        self.add("POST", F + r"/prompt", self.post_prompt)
        self.add("POST", F + r"/marks", self.post_mark)
        self.add("POST", F + r"/close-request", self.post_close_request)
        self.add("GET", F + r"/export", self.get_export)

    # --- identity -----------------------------------------------------------------------------

    def user_name(self, req: Request) -> str:
        return (req.user or {}).get("name") or local_user()

    def can_write(self, req: Request, pid: str) -> None:
        """Hub overrides this with project roles."""

    # --- views -------------------------------------------------------------------------------

    def get_meta(self, req: Request) -> Response:
        meta = self.api.meta(req.user)
        meta["generation"] = self.generation
        meta["review"] = True
        return Response.json(meta)

    def feature_view(self, req: Request, pid: str, fid: str) -> dict:
        data = self.api.feature(pid, fid)
        user = self.user_name(req)
        feature_dir = self.api.feature_dir(pid, fid)
        evidence = util.read_json(feature_dir / "evidence" / "evidence.json", {}) or {}
        data["comments"] = comments.locate(comments.load_all(pid, fid, self.api.root, user), feature_dir, evidence)
        hunk_ids = [h["id"] for f in evidence.get("files") or [] for h in f.get("hunks", [])]
        mine = comments.marks(pid, fid, user, self.api.root)
        data["marks"] = [h for h in mine if h in set(hunk_ids)]
        data["review"] = {"reviewed": len(data["marks"]), "total": len(hunk_ids), "user": user}
        return data

    def get_comments(self, req: Request, pid: str, fid: str) -> Response:
        feature_dir = self.api.feature_dir(pid, fid)
        evidence = util.read_json(feature_dir / "evidence" / "evidence.json", {}) or {}
        found = comments.locate(comments.load_all(pid, fid, self.api.root, self.user_name(req)), feature_dir, evidence)
        return Response.json({"comments": found})

    def _bump(self) -> int:
        """Count a change; write responses carry the count so the writer's viewer doesn't report its own change."""
        self.generation += 1
        return self.generation

    def post_comment(self, req: Request, pid: str, fid: str) -> Response:
        self.api.feature_dir(pid, fid)
        self.can_write(req, pid)
        try:
            comment = comments.create(pid, fid, req.json_body(), self.user_name(req), self.api.root)
        except comments.CommentError as exc:
            raise BadRequest(str(exc)) from None
        if comment["visibility"] == "shared":
            publish_records(pid, f"Add a review comment on {fid}", self.api.root)
        return Response.json({"comment": comment, "generation": self._bump()}, 201)

    def patch_comment(self, req: Request, pid: str, fid: str, cid: str) -> Response:
        self.api.feature_dir(pid, fid)
        self.can_write(req, pid)
        try:
            comment, shared = comments.update(pid, fid, cid, req.json_body(), self.user_name(req), self.api.root)
        except comments.CommentError as exc:
            raise (NotFound if "no such" in str(exc) else BadRequest)(str(exc)) from None
        if shared or comment["visibility"] == "shared":
            publish_records(pid, f"Update a review comment on {fid}", self.api.root)
        return Response.json({"comment": comment, "generation": self._bump()})

    def delete_comment(self, req: Request, pid: str, fid: str, cid: str) -> Response:
        self.api.feature_dir(pid, fid)
        self.can_write(req, pid)
        try:
            shared = comments.delete(pid, fid, cid, self.user_name(req), self.api.root)
        except comments.CommentError as exc:
            raise NotFound(str(exc)) from None
        if shared:
            publish_records(pid, f"Delete a review comment on {fid}", self.api.root)
        return Response.json({"deleted": cid, "generation": self._bump()})

    def post_prompt(self, req: Request, pid: str, fid: str) -> Response:
        feature_dir = self.api.feature_dir(pid, fid)
        self.can_write(req, pid)
        body = req.json_body()
        user = self.user_name(req)
        evidence = util.read_json(feature_dir / "evidence" / "evidence.json", {}) or {}
        found = comments.locate(comments.load_all(pid, fid, self.api.root, user), feature_dir, evidence)
        ids = body.get("ids")
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
            raise BadRequest("ids must be a list of comment ids")
        if ids:
            chosen = [c for c in found if c["id"] in set(ids)]
        else:
            chosen = [c for c in found if c.get("state") == "open"]
        if not chosen:
            raise BadRequest("no comments to include: select some, or leave open comments to send")
        legs = records.load_legs(feature_dir)
        feature = {"feature_id": fid, "branch": legs[-1].get("branch") if legs else None}
        prompt = comments.build_prompt(chosen, feature, evidence.get("head_commit") or evidence.get("head"))
        result = {"prompt": prompt, "count": len(chosen), "ids": [c["id"] for c in chosen], "queued": False}
        changed = comments.mark_sent(pid, fid, result["ids"], user, self.api.root)
        if body.get("queue"):
            path = comments.queue_feedback(pid, fid, prompt, user, self.api.root)
            result.update(queued=True, feedback=str(path))
            changed = True
        if changed:
            publish_records(pid, f"Queue review feedback for {fid}" if body.get("queue") else f"Send review comments on {fid}",
                            self.api.root)
        result["generation"] = self._bump()
        return Response.json(result)

    def post_mark(self, req: Request, pid: str, fid: str) -> Response:
        self.api.feature_dir(pid, fid)
        body = req.json_body()
        try:
            mine = comments.set_mark(pid, fid, self.user_name(req), str(body.get("hunk_id") or ""),
                                     bool(body.get("reviewed", True)), self.api.root)
        except comments.CommentError as exc:
            raise BadRequest(str(exc)) from None
        return Response.json({"marks": list(mine)})

    def post_close_request(self, req: Request, pid: str, fid: str) -> Response:
        self.api.feature_dir(pid, fid)
        self.can_write(req, pid)
        result = request_close(pid, fid, self.user_name(req), req.json_body().get("note"), self.api.root)
        publish_records(pid, f"Request close of {result['leg_id']} of {fid}", self.api.root)
        result["generation"] = self._bump()
        return Response.json(result)

    def get_export(self, req: Request, pid: str, fid: str) -> Response:
        from . import export

        html = export.render(self.api, pid, fid)
        name = f"debrief-{fid}.html"
        return Response(200, html.encode("utf-8"), "text/html; charset=utf-8", {
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            # The download's own CSP (in its meta tag) applies when it is opened.
        })


def cli_request_close(args) -> int:
    root = paths.archive_root()
    pid = args.project
    fid = args.feature
    if not pid or not fid:
        found = projects.find_project(Path.cwd())
        if found and not fid:
            try:
                ctx = session.Context(Path.cwd())
                pid, fid = ctx.project_id, ctx.feature_id
            except session.Untracked:
                pass
        elif found:
            pid = pid or found[0]
    if fid and "/" in fid:
        fid = util.feature_id_for_branch(fid)
    if not pid or not fid:
        print("Run this inside the repo on the feature's branch, or pass the feature and --project.")
        return 1
    try:
        result = request_close(pid, fid, local_user(), args.note, root)
    except BadRequest as exc:
        print(f"debrief request-close: {exc}")
        return 1
    publish_records(pid, f"Request close of {result['leg_id']} of {fid}", root, wait=True)
    print(f"Requested close of {result['leg_id']}. The agent sees it at its next `bin/session now`.")
    print("Or paste this into the agent:")
    print(f"  {result['prompt']}")
    return 0
