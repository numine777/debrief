"""``debrief serve``: the local viewer on 127.0.0.1.

Standard library only: ``ThreadingHTTPServer`` serves the vendored front end
and the ``/api/v1`` JSON views. Every response carries a content security
policy that allows only same-origin requests (goal G4), the Host header must
name this server (no DNS rebinding), and every write needs the ``X-Debrief``
header and a same-origin Origin (no cross-site requests).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, paths, resources, util
from .api import Api, BadRequest, NotFound

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8", ".json": "application/json",
}
MAX_BODY = 2 * 1024 * 1024


class Response:
    def __init__(self, status: int, body: bytes = b"", content_type: str = "application/json",
                 headers: Optional[Dict[str, str]] = None):
        self.status = status
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.headers.update(headers or {})

    @classmethod
    def json(cls, data, status: int = 200) -> "Response":
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return cls(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    @classmethod
    def error(cls, status: int, message: str) -> "Response":
        return cls.json({"error": message}, status)


class Request:
    def __init__(self, method: str, target: str, headers: Dict[str, str], body: bytes = b"",
                 client: str = "127.0.0.1"):
        self.method = method.upper()
        parts = urlsplit(target)
        self.path = unquote(parts.path)
        self.query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body
        self.client = client
        self.user: Optional[dict] = None

    def json_body(self) -> dict:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("request body must be a JSON object")
        return data


Route = Tuple[str, "re.Pattern[str]", Callable]


class App:
    """Routes requests to the API. The hub subclasses it to add sign-in."""

    def __init__(self, api: Api, port: int, allowed_hosts: Optional[List[str]] = None):
        self.api = api
        self.port = port
        self.allowed_hosts = set(allowed_hosts or [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"])
        self._static_cache: Dict[str, Tuple[bytes, str]] = {}
        self._ingest_lock = threading.Lock()
        self.routes: List[Route] = []
        F = r"/api/v1/projects/(?P<pid>[^/]+)/features/(?P<fid>[^/]+)"
        self.add("GET", r"/api/v1/meta", self.get_meta)
        self.add("GET", r"/api/v1/index", lambda req: Response.json(self.api.index_view()))
        self.add("GET", F, lambda req, pid, fid: Response.json(self.feature_view(req, pid, fid)))
        self.add("GET", F + r"/diff", lambda req, pid, fid: Response.json(
            self.api.diff(pid, fid, req.query.get("scope", "feature"))))
        self.add("GET", F + r"/blobs/(?P<blob>[0-9a-f]{7,64})", self.get_blob)
        self.add("POST", F + r"/ingest", self.post_ingest)
        self.add("GET", r"/api/v1/commits/(?P<sha>[0-9a-fA-F]{4,64})", lambda req, sha: Response.json(self.api.commit(sha)))
        self.add("GET", r"/api/v1/search", lambda req: Response.json(self.api.search(req.query.get("q", ""))))
        self.add("GET", r"/api/v1/epics/(?P<name>[^/]+)", lambda req, name: Response.json(self.api.epic(name)))
        self.extend_routes(F)

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), handler))

    def extend_routes(self, feature_prefix: str) -> None:
        """Hook for review-loop and hub routes."""

    # --- request handling ---------------------------------------------------------

    def handle(self, req: Request) -> Response:
        try:
            denied = self.check_request(req)
            if denied is not None:
                return self.finish(req, denied)
            if req.path == "/" or req.path == "/index.html":
                return self.finish(req, self.static("index.html"))
            if req.path in ("/favicon.ico", "/favicon.svg"):
                return self.finish(req, self.static("favicon.svg"))
            if req.path.startswith("/static/"):
                if req.method not in ("GET", "HEAD"):
                    return self.finish(req, Response.error(405, "method not allowed"))
                return self.finish(req, self.static(req.path[len("/static/"):]))
            for method, pattern, handler in self.routes:
                match = pattern.match(req.path)
                if match and (method == req.method or (method == "GET" and req.method == "HEAD")):
                    return self.finish(req, handler(req, **match.groupdict()))
            if any(p.match(req.path) for _m, p, _h in self.routes):
                return self.finish(req, Response.error(405, "method not allowed"))
            return self.finish(req, Response.error(404, "not found"))
        except NotFound as exc:
            return self.finish(req, Response.error(404, str(exc)))
        except BadRequest as exc:
            return self.finish(req, Response.error(400, str(exc)))
        except PermissionError as exc:
            return self.finish(req, Response.error(403, str(exc)))
        except Exception as exc:  # report, never crash the server thread
            return self.finish(req, Response.error(500, f"{type(exc).__name__}: {exc}"))

    def check_request(self, req: Request) -> Optional[Response]:
        host = req.headers.get("host", "")
        if host not in self.allowed_hosts:
            return Response.error(421, f"Debrief only answers requests for {sorted(self.allowed_hosts)[0]}")
        if req.method in ("POST", "PATCH", "PUT", "DELETE"):
            if req.headers.get("x-debrief") != "1":
                return Response.error(403, "missing X-Debrief header")
            origin = req.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != host:
                return Response.error(403, "cross-origin request refused")
            if self.api.readonly:
                return Response.error(403, "this viewer is read-only")
        return None

    def finish(self, req: Request, resp: Response) -> Response:
        resp.headers.update(SECURITY_HEADERS)
        accepts_gzip = "gzip" in req.headers.get("accept-encoding", "")
        if accepts_gzip and len(resp.body) > 16384 and resp.headers["Content-Type"].split(";")[0] in (
                "application/json", "text/javascript", "text/css", "text/html", "text/plain"):
            resp.body = gzip.compress(resp.body, compresslevel=5)
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Vary"] = "Accept-Encoding"
        return resp

    def static(self, rel: str) -> Response:
        rel = rel.lstrip("/")
        if ".." in rel.split("/") or rel.startswith("."):
            return Response.error(404, "not found")
        cached = self._static_cache.get(rel)
        if cached is None:
            try:
                data = resources.read_bytes(f"static/{rel}")
            except (FileNotFoundError, OSError):
                return Response.error(404, "not found")
            ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
            ctype = STATIC_TYPES.get(ext) or mimetypes.guess_type(rel)[0] or "application/octet-stream"
            cached = (data, ctype)
            self._static_cache[rel] = cached
        data, ctype = cached
        etag = '"' + hashlib.sha1(data).hexdigest()[:16] + '"'
        cache = "no-cache" if rel == "index.html" else "public, max-age=300"
        return Response(200, data, ctype, {"ETag": etag, "Cache-Control": cache})

    # --- handlers --------------------------------------------------------------------

    def get_meta(self, req: Request) -> Response:
        return Response.json(self.api.meta(req.user))

    def feature_view(self, req: Request, pid: str, fid: str) -> dict:
        return self.api.feature(pid, fid)

    def get_blob(self, req: Request, pid: str, fid: str, blob: str) -> Response:
        return Response(200, self.api.blob(pid, fid, blob), "text/plain; charset=utf-8",
                        {"Cache-Control": "public, max-age=86400"})

    def post_ingest(self, req: Request, pid: str, fid: str) -> Response:
        from . import ingest

        self.api.feature_dir(pid, fid)
        if not self._ingest_lock.acquire(timeout=60):
            return Response.error(409, "another ingest is still running")
        try:
            evidence = ingest.ingest_feature(pid, fid, root=self.api.root)
        finally:
            self._ingest_lock.release()
        return Response.json({"summary": ingest.summary_line(evidence), "computed_at": evidence.get("computed_at")})


class Handler(BaseHTTPRequestHandler):
    server_version = f"Debrief/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.send_error(413, "request body too large")
            return
        body = self.rfile.read(length) if length else b""
        req = Request(self.command, self.path, dict(self.headers.items()), body, self.client_address[0])
        resp = self.server.app.handle(req)  # type: ignore[attr-defined]
        self.send_response(resp.status)
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp.body)

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = do_HEAD = _dispatch

    def log_message(self, fmt: str, *args) -> None:
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: App, verbose: bool = False):
        super().__init__(address, Handler)
        self.app = app
        self.verbose = verbose


def make_server(port: int, root=None, host: str = "127.0.0.1", readonly: bool = False) -> Server:
    from .review import ReviewApp

    api = Api(root, mode="local", readonly=readonly)
    return Server((host, port), ReviewApp(api, port))


def cli(args) -> int:
    from . import config, ingest

    cfg = config.load()
    port = args.port or cfg.port
    try:
        server = make_server(port)
    except OSError as exc:
        print(f"Can't listen on 127.0.0.1:{port}: {exc.strerror or exc}. Pick another port with --port.")
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"Debrief {__version__} serving {paths.archive_root()} at {url}")
    print("Only this machine can connect. From another machine, forward the port: ssh -L "
          f"{port}:127.0.0.1:{port} <this host>")
    if not args.no_ingest:
        def startup_ingest() -> None:
            for pid in __import__("debrief.projects", fromlist=["x"]).list_projects():
                for fid in ingest.feature_ids(pid):
                    try:
                        ingest.ingest_feature(pid, fid)
                    except Exception as exc:  # keep serving; report the feature
                        print(f"ingest {pid}/{fid} failed: {exc}", file=sys.stderr)
        threading.Thread(target=startup_ingest, name="startup-ingest", daemon=True).start()
    if getattr(args, "watch", False):
        try:
            from . import watch

            watch.start(server.app, cfg)
            print("Watching registered repos and archives for changes.")
        except ImportError:
            print("--watch is not available in this build.")
    if getattr(args, "open", False):
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
