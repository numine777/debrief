"""``debrief hub``: the shared deployment of the viewer (phase 1, decision D8).

The hub serves the same front end and ``/api/v1`` as ``debrief serve``, over
TLS, to signed-in teammates. Hosts sync each project's archive to a bare git
repository the hub owns (over SSH, with existing keys); the hub merges them
into working clones it reads from, and pushes its own writes (shared comments,
replies, queued feedback, close requests) back, so they reach the developer's
host through ordinary archive sync. The hub holds no project repositories and
makes no outbound connections.

Layout of the hub directory::

    hub.json              bind address, port, TLS files, host names
    users.json            users with hashed access tokens
    projects.json         project members and roles (owner, reviewer, reader)
    repos/<project>.git   bare archive repositories hosts push to
    archive/projects/...  working clones the viewer reads
    archive/.viewer/      per-user private comments and review marks
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, archive, gitutil, index, paths, util
from .api import Api, BadRequest, NotFound
from .review import ReviewApp
from .server import Request, Response, Server

ROLES = ("reader", "reviewer", "owner")
SESSION_SECONDS = 12 * 3600
COOKIE = "debrief_session"
DEFAULT_PORT = 7320


def hub_dir(value: Optional[str] = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    env = os.environ.get("DEBRIEF_HUB_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return paths.tool_home().parent / "debrief-hub"


class Hub:
    """Users, tokens, roles and repositories in one hub directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.archive_root = self.root / "archive"
        self.lock = threading.RLock()

    # --- files ------------------------------------------------------------------------

    def _read(self, name: str, default: dict) -> dict:
        data = util.read_json(self.root / name, None)
        return data if isinstance(data, dict) else default

    def _write(self, name: str, data: dict) -> None:
        util.write_json(self.root / name, data)
        try:
            os.chmod(self.root / name, 0o600)
        except OSError:
            pass

    @property
    def config(self) -> dict:
        return self._read("hub.json", {})

    def init(self, hostname: Optional[str] = None) -> List[str]:
        report = []
        for sub in ("repos", "archive/projects"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if not (self.root / "hub.json").exists():
            host = hostname or socket.getfqdn()
            self._write("hub.json", {"bind": "127.0.0.1", "port": DEFAULT_PORT, "cert": None, "key": None,
                                     "hostnames": [host], "created_at": util.now_iso()})
            report.append(f"Created {self.root / 'hub.json'} (bind 127.0.0.1:{DEFAULT_PORT}; set bind to serve your network)")
        for name, default in (("users.json", {"users": {}}), ("projects.json", {"projects": {}})):
            if not (self.root / name).exists():
                self._write(name, default)
        cert, key = self.root / "tls" / "cert.pem", self.root / "tls" / "key.pem"
        cfg = self.config
        if not cfg.get("cert") and shutil.which("openssl"):
            cert.parent.mkdir(exist_ok=True)
            host = (cfg.get("hostnames") or [socket.getfqdn()])[0]
            proc = subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825", "-subj", f"/CN={host}",
                 "-addext", f"subjectAltName=DNS:{host},DNS:localhost,IP:127.0.0.1",
                 "-keyout", str(key), "-out", str(cert)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
            if proc.returncode == 0:
                os.chmod(key, 0o600)
                cfg.update(cert=str(cert), key=str(key))
                self._write("hub.json", cfg)
                report.append(f"Generated a self-signed certificate for {host} in {cert.parent}. "
                              "Replace it with one from your internal CA for teammates' browsers to trust it.")
        if not self.config.get("cert"):
            report.append("No TLS certificate yet: set cert and key in hub.json (or pass --cert/--key).")
        return report

    # --- users and tokens -----------------------------------------------------------------

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def add_user(self, name: str, admin: bool = False) -> str:
        if not name or not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise BadRequest("user names use letters, digits, '.', '-' and '_'")
        with self.lock:
            data = self._read("users.json", {"users": {}})
            if name in data["users"]:
                raise BadRequest(f"{name} already exists; use `debrief hub token {name}` for a new token")
            data["users"][name] = {"created_at": util.now_iso(), "admin": bool(admin), "tokens": []}
            self._write("users.json", data)
        return self.new_token(name, "initial")

    def new_token(self, name: str, label: str = "") -> str:
        token = "dbh_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        with self.lock:
            data = self._read("users.json", {"users": {}})
            user = data["users"].get(name)
            if user is None:
                raise NotFound(f"no user {name}")
            user["tokens"].append({"id": secrets.token_hex(4), "hash": self._hash(token), "label": label,
                                   "created_at": util.now_iso(), "last_used": None})
            self._write("users.json", data)
        return token

    def revoke_tokens(self, name: str) -> int:
        with self.lock:
            data = self._read("users.json", {"users": {}})
            user = data["users"].get(name)
            if user is None:
                raise NotFound(f"no user {name}")
            count = len(user["tokens"])
            user["tokens"] = []
            self._write("users.json", data)
        return count

    def authenticate(self, token: str) -> Optional[dict]:
        if not token or not token.startswith("dbh_"):
            return None
        digest = self._hash(token)
        with self.lock:
            data = self._read("users.json", {"users": {}})
            for name, user in data["users"].items():
                for entry in user.get("tokens", []):
                    if hmac.compare_digest(entry["hash"], digest):
                        entry["last_used"] = util.now_iso()
                        self._write("users.json", data)
                        return {"name": name, "admin": bool(user.get("admin"))}
        return None

    def users(self) -> Dict[str, dict]:
        return self._read("users.json", {"users": {}})["users"]

    # --- projects and roles -------------------------------------------------------------------

    def grant(self, user: str, project_id: str, role: str) -> None:
        if role not in ROLES:
            raise BadRequest(f"role must be one of {', '.join(ROLES)}")
        if user not in self.users():
            raise NotFound(f"no user {user}")
        with self.lock:
            data = self._read("projects.json", {"projects": {}})
            data["projects"].setdefault(project_id, {"members": {}})["members"][user] = role
            self._write("projects.json", data)

    def revoke(self, user: str, project_id: str) -> bool:
        with self.lock:
            data = self._read("projects.json", {"projects": {}})
            members = data["projects"].get(project_id, {}).get("members", {})
            found = members.pop(user, None) is not None
            self._write("projects.json", data)
        return found

    def role(self, user: Optional[dict], project_id: str) -> Optional[str]:
        if not user:
            return None
        if user.get("admin"):
            return "owner"
        members = self._read("projects.json", {"projects": {}})["projects"].get(project_id, {}).get("members", {})
        return members.get(user["name"])

    def readable_projects(self, user: Optional[dict]) -> List[str]:
        ids = [p.name for p in (self.archive_root / "projects").iterdir() if p.is_dir()] \
            if (self.archive_root / "projects").is_dir() else []
        return sorted(pid for pid in ids if self.role(user, pid))

    # --- repositories --------------------------------------------------------------------

    def repo_path(self, project_id: str) -> Path:
        return self.root / "repos" / f"{project_id}.git"

    def add_repo(self, project_id: str) -> Path:
        """Create the bare repository hosts push to, and the working clone the viewer reads."""
        from .api import check_id

        check_id(project_id, "project id")
        bare = self.repo_path(project_id)
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            gitutil.run(["init", "-q", "--bare", "-b", "main", str(bare)], self.root)
        self.ensure_clone(project_id)
        return bare

    def ensure_clone(self, project_id: str) -> Path:
        clone = paths.project_dir(project_id, self.archive_root)
        if not archive.is_repo(clone):
            archive.ensure_repo(clone)
            archive._git(clone, ["remote", "add", "origin", str(self.repo_path(project_id))])
            archive.pull(clone)
        return clone

    def repo_projects(self) -> List[str]:
        base = self.root / "repos"
        if not base.is_dir():
            return []
        return sorted(p.name[:-4] for p in base.iterdir() if p.name.endswith(".git"))


# --- the hub server ------------------------------------------------------------------------------------


class HubApp(ReviewApp):
    """The viewer with sign-in and project roles."""

    def __init__(self, hub: Hub, port: int, hostnames: Optional[List[str]] = None, secure: bool = True):
        self.hub = hub
        self.secure = secure
        self.sessions: Dict[str, dict] = {}
        self.failures: Dict[str, List[float]] = {}
        api = Api(hub.archive_root, mode="hub")
        hosts = []
        for name in hostnames or []:
            hosts += [name, f"{name}:{port}"]
        super().__init__(api, port, hosts or None)
        self.any_host = not hostnames

    def extend_routes(self, F: str) -> None:
        super().extend_routes(F)
        self.add("POST", r"/api/v1/login", self.post_login)
        self.add("POST", r"/api/v1/logout", self.post_logout)

    # --- identity -----------------------------------------------------------------------------

    def _session_user(self, req: Request) -> Optional[dict]:
        auth = req.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return self.hub.authenticate(auth[7:].strip())
        cookies = req.headers.get("cookie", "")
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and value:
                session = self.sessions.get(value)
                if session and session["expires"] > time.time():
                    return session["user"]
        return None

    def check_request(self, req: Request) -> Optional[Response]:
        if not self.any_host and req.headers.get("host", "") not in self.allowed_hosts:
            return Response.error(421, "this hub answers only for its configured host names")
        req.user = self._session_user(req)
        if req.method in ("POST", "PATCH", "PUT", "DELETE"):
            if req.headers.get("x-debrief") != "1":
                return Response.error(403, "missing X-Debrief header")
            origin = req.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != req.headers.get("host", ""):
                return Response.error(403, "cross-origin request refused")
        public = req.path in ("/", "/index.html", "/favicon.ico", "/favicon.svg", "/api/v1/meta", "/api/v1/login") \
            or req.path.startswith("/static/")
        if not public and req.user is None:
            return Response.error(401, "sign in with your access token")
        return None

    def finish(self, req: Request, resp: Response) -> Response:
        resp = super().finish(req, resp)
        if self.secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def user_name(self, req: Request) -> str:
        return (req.user or {}).get("name") or "anonymous"

    def require(self, req: Request, pid: str, write: bool = False) -> None:
        role = self.hub.role(req.user, pid)
        if role is None:
            raise NotFound(f"no project {pid} you can see")
        if write and role == "reader":
            raise PermissionError("readers can't change review records; ask an owner for the reviewer role")

    def can_write(self, req: Request, pid: str) -> None:
        self.require(req, pid, write=True)

    # --- sign-in ---------------------------------------------------------------------------------

    def post_login(self, req: Request) -> Response:
        recent = [t for t in self.failures.get(req.client, []) if t > time.time() - 600]
        if len(recent) >= 10:
            return Response.error(429, "too many failed sign-ins; wait ten minutes")
        token = str(req.json_body().get("token") or "").strip()
        user = self.hub.authenticate(token)
        if user is None:
            recent.append(time.time())
            self.failures[req.client] = recent
            time.sleep(0.3)
            return Response.error(401, "that token isn't valid")
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = {"user": user, "expires": time.time() + SESSION_SECONDS}
        cookie = f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}"
        if self.secure:
            cookie += "; Secure"
        return Response(200, b'{"user":' + util_json(user) + b"}", "application/json; charset=utf-8",
                        {"Set-Cookie": cookie, "Cache-Control": "no-store"})

    def post_logout(self, req: Request) -> Response:
        for part in req.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                self.sessions.pop(value, None)
        return Response(200, b'{"signed_out":true}', "application/json; charset=utf-8",
                        {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})

    # --- views with access control ------------------------------------------------------------

    def get_meta(self, req: Request) -> Response:
        meta = self.api.meta(req.user)
        meta.update(generation=self.generation, review=True, hub=True, version=__version__)
        return Response.json(meta)

    def handle(self, req: Request) -> Response:
        # Route-level checks for project-scoped reads; writes check roles in their handlers.
        parts = req.path.split("/")
        if req.path.startswith("/api/v1/projects/") and len(parts) > 4:
            denied = self.check_request(req)
            if denied is not None:
                return self.finish(req, denied)
            try:
                self.require(req, parts[4])
            except NotFound as exc:
                return self.finish(req, Response.error(404, str(exc)))
        return super().handle(req)

    def feature_view(self, req: Request, pid: str, fid: str) -> dict:
        self.require(req, pid)
        data = super().feature_view(req, pid, fid)
        data["role"] = self.hub.role(req.user, pid)
        return data

    def route_index(self, req: Request) -> Response:
        view = self.api.index_view()
        readable = set(self.hub.readable_projects(req.user))
        view["projects"] = [p for p in view["projects"] if p["project_id"] in readable]
        epics: Dict[str, int] = {}
        for p in view["projects"]:
            for f in p["features"]:
                if f.get("epic"):
                    epics[f["epic"]] = epics.get(f["epic"], 0) + 1
        view["epics"] = epics
        return Response.json(view)

    def route_commit(self, req: Request, sha: str) -> Response:
        data = self.api.commit(sha)
        readable = set(self.hub.readable_projects(req.user))
        data["features"] = [f for f in data["features"] if f["project_id"] in readable]
        data["landed"] = [f for f in data["landed"] if f["project_id"] in readable]
        if not data["features"] and not data["landed"]:
            raise NotFound(f"no feature you can see contains {sha}")
        return Response.json(data)

    def route_search(self, req: Request) -> Response:
        data = self.api.search(req.query.get("q", ""))
        readable = set(self.hub.readable_projects(req.user))
        data["results"] = [r for r in data["results"] if r["project_id"] in readable]
        return Response.json(data)

    def route_epic(self, req: Request, name: str) -> Response:
        data = self.api.epic(name)
        readable = set(self.hub.readable_projects(req.user))
        data["features"] = [f for f in data["features"] if f["project_id"] in readable]
        if not data["features"]:
            raise NotFound(f"no features in epic {name}")
        return Response.json(data)

    def post_ingest(self, req: Request, pid: str, fid: str) -> Response:
        """On the hub, Refresh pulls the project's archive instead of reading a repo."""
        self.require(req, pid)
        clone = self.hub.ensure_clone(pid)
        result = archive.sync(clone, "Sync hub records")
        index.update_feature(pid, fid, root=self.hub.archive_root)
        return Response.json({"summary": "pulled the latest records" if result.get("pulled") else
                              f"records unchanged ({result.get('reason', 'up to date')})"})

    def get_blob(self, req: Request, pid: str, fid: str, blob: str) -> Response:
        self.require(req, pid)
        return super().get_blob(req, pid, fid, blob)


def util_json(data) -> bytes:
    import json

    return json.dumps(data).encode("utf-8")


def _rewire_routes(app: HubApp) -> None:
    """Swap the generic read routes for role-filtered ones."""
    replacements = {
        r"^/api/v1/index$": app.route_index,
        r"^/api/v1/commits/(?P<sha>[0-9a-fA-F]{4,64})$": app.route_commit,
        r"^/api/v1/search$": app.route_search,
        r"^/api/v1/epics/(?P<name>[^/]+)$": app.route_epic,
    }
    routes = []
    for method, pattern, handler in app.routes:
        routes.append((method, pattern, replacements.get(pattern.pattern, handler)))
    app.routes = routes


# --- syncing archives from hosts --------------------------------------------------------------


class HubSyncer:
    """Merge what hosts push into the working clones, and push hub writes back."""

    def __init__(self, hub: Hub, app: Optional[HubApp] = None, interval: float = 5.0):
        self.hub = hub
        self.app = app
        self.interval = interval
        self.seen: Dict[str, Dict[str, str]] = {}
        self.stop_event = threading.Event()

    def tick(self) -> List[str]:
        updated = []
        for pid in self.hub.repo_projects():
            refs = gitutil.refs(self.hub.repo_path(pid))
            clone = self.hub.ensure_clone(pid)
            if refs == self.seen.get(pid) and not archive.unpushed(clone):
                continue
            self.seen[pid] = refs
            try:
                archive.sync(clone, "Merge records on the hub")
            except Exception as exc:  # report and retry next tick
                print(f"debrief hub: syncing {pid} failed: {exc}", file=sys.stderr)
                continue
            features = clone / "features"
            if features.is_dir():
                for fdir in features.iterdir():
                    if fdir.is_dir():
                        index.update_feature(pid, fdir.name, root=self.hub.archive_root)
            self.seen[pid] = gitutil.refs(self.hub.repo_path(pid))
            updated.append(pid)
        if updated and self.app is not None:
            self.app.generation += 1
        return updated

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"debrief hub: sync pass failed: {exc}", file=sys.stderr)
            self.stop_event.wait(self.interval)


# --- serving -------------------------------------------------------------------------------------


class HubServer(Server):
    """Threaded server whose TLS handshakes happen in worker threads, with a deadline.

    Wrapping the listening socket would handshake inside accept(), so one slow
    client could stall every other connection.
    """

    handshake_timeout = 15.0

    def __init__(self, address, app, context: Optional[ssl.SSLContext]):
        super().__init__(address, app)
        self.ssl_context = context

    def finish_request(self, request, client_address):
        if self.ssl_context is not None:
            request.settimeout(self.handshake_timeout)
            try:
                request = self.ssl_context.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError):
                request.close()
                return
            request.settimeout(60)
            try:
                super().finish_request(request, client_address)
            finally:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                request.close()
            return
        super().finish_request(request, client_address)


def make_hub_server(hub: Hub, bind: str, port: int, cert: Optional[str], key: Optional[str],
                    insecure_http: bool = False, hostnames: Optional[List[str]] = None) -> Server:
    secure = not insecure_http
    context = None
    if secure:
        if not cert or not key:
            raise BadRequest("the hub serves over TLS: set cert and key in hub.json or pass --cert and --key")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
    app = HubApp(hub, port, hostnames, secure=secure)
    _rewire_routes(app)
    return HubServer((bind, port), app, context)


def cli(args) -> int:
    hub = Hub(hub_dir(args.dir))
    cmd = args.hub_cmd
    extra = list(args.args or [])
    try:
        if cmd == "init":
            for line in hub.init(extra[0] if extra else None):
                print(line)
            print(f"Hub directory: {hub.root}")
            return 0
        if not (hub.root / "hub.json").exists():
            print(f"No hub at {hub.root}; run `debrief hub init` first (or pass --dir).")
            return 1
        if cmd == "adduser":
            if not extra:
                print("usage: debrief hub adduser <name> [--role ...]")
                return 2
            token = hub.add_user(extra[0], admin=bool(getattr(args, "admin", False)))
            print(f"Created {extra[0]}. Their access token (shown once; store it in a password manager):")
            print(f"  {token}")
            return 0
        if cmd == "token":
            token = hub.new_token(extra[0], "cli")
            print(f"New token for {extra[0]} (shown once):\n  {token}")
            return 0
        if cmd == "grant":
            user, pid = extra[0], extra[1]
            hub.grant(user, pid, args.role or "reviewer")
            print(f"{user} is now {args.role or 'reviewer'} of {pid}.")
            return 0
        if cmd == "revoke":
            if len(extra) == 1:
                print(f"Revoked {hub.revoke_tokens(extra[0])} token(s) of {extra[0]}.")
            else:
                print("Removed." if hub.revoke(extra[0], extra[1]) else "Nothing to remove.")
            return 0
        if cmd == "users":
            roles = hub._read("projects.json", {"projects": {}})["projects"]
            for name, user in sorted(hub.users().items()):
                member = [f"{pid} ({p['members'][name]})" for pid, p in roles.items() if name in p.get("members", {})]
                print(f"{name}{' (admin)' if user.get('admin') else ''}: {len(user.get('tokens', []))} token(s); "
                      + (", ".join(member) or "no projects"))
            return 0
        if cmd == "repo":
            pid = extra[0]
            bare = hub.add_repo(pid)
            host = (hub.config.get("hostnames") or [socket.getfqdn()])[0]
            print(f"Bare archive repository: {bare}")
            print("On each host, point the project's archive at it (SSH with your usual keys):")
            print(f"  debrief sync {pid} --remote ssh://{host}{bare}")
            print(f"and add {host} to [residency] allow_hosts in ~/.config/debrief/config on those hosts.")
            return 0
        if cmd == "serve":
            cfg = hub.config
            bind = args.bind or cfg.get("bind") or "127.0.0.1"
            port = args.port or cfg.get("port") or DEFAULT_PORT
            cert = args.cert or cfg.get("cert")
            key = args.key or cfg.get("key")
            if args.insecure_http and bind not in ("127.0.0.1", "localhost", "::1"):
                print("Refusing plain HTTP on a network address; it is for local testing only.")
                return 1
            server = make_hub_server(hub, bind, port, cert, key, args.insecure_http, cfg.get("hostnames"))
            syncer = HubSyncer(hub, server.app)
            threading.Thread(target=syncer.run, name="hub-sync", daemon=True).start()
            scheme = "http" if args.insecure_http else "https"
            print(f"Debrief Hub {__version__} serving {hub.archive_root} at {scheme}://{bind}:{port}/")
            print("Sign in with an access token from `debrief hub adduser`. The hub makes no outbound connections.")
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                print("\nStopped.")
            finally:
                syncer.stop_event.set()
                server.server_close()
            return 0
    except (BadRequest, NotFound) as exc:
        print(f"debrief hub {cmd}: {exc}")
        return 1
    except IndexError:
        print(f"debrief hub {cmd}: missing arguments")
        return 2
    print(f"Unknown hub command {cmd}.")
    return 2
