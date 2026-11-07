"""Local-only, dependency-free HTTP/SSE frontend for the shared Agent core."""

import argparse
import errno
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import hashlib
import os
from pathlib import Path
import secrets
import signal
import threading
import time
import re
import urllib.parse

from multiagent.image_reader import MAX_IMAGE_BYTES
from session.config import default_env_file, load_env_file
from session import SavedSession
from session.store import UnsupportedSession
from session.hosting import SessionBusyError
from .host import GuiSessionHost, GuiBusyError
from multiagent.agent import preview
from .progress import ProgressRegistry
from .status import WebStatus
from .streams import PageStreams
from multiagent.media import AgentMedia
from .lifecycle import replace_listener
from multiagent.model_config import public_profiles, UNKNOWN_COLORS
from multiagent.model_config import api_key, default_model, model_choices
from multiagent.runtime_build import RUNTIME_REVISION


STATIC = Path(__file__).with_name("static")
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/rendering.js": ("rendering.js", "text/javascript; charset=utf-8"),
          "/statistics.js": ("statistics.js", "text/javascript; charset=utf-8"),
          "/all-agents.js": ("all-agents.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/logo.svg": ("logo.svg", "image/svg+xml"),
          "/logomark.svg": ("logomark.svg", "image/svg+xml")}
for asset in (STATIC / "vendor").rglob("*"):
    if asset.is_file() and not asset.is_symlink() and asset.suffix in {".js", ".css", ".woff", ".woff2", ".ttf"}:
        relative = asset.relative_to(STATIC).as_posix()
        ASSETS["/" + relative] = (relative, mimetypes.guess_type(relative)[0] or "application/octet-stream")

API_VERSION = 4
CAPABILITIES = {"session_delete": True, "session_metadata": True, "sub_agents": True,
                "clipboard_upload": True, "image_assets": True, "model_switching": True,
                "global_agent_model": True, "live_agent_input": True, "workspace_switch": True,
                "session_lifetime_statistics": True, "persistent_agent_loops": True,
                "queued_input_only": True, "peer_messages": True, "human_outbox": True,
                "display_only_images": True, "configurable_idle_sleep": True,
                "session_message_feed": True, "all_agents_view": True, "human_broadcast": True,
                "gui_release": True, "gui_takeover": True}


def accepts_gzip(value):
    qualities = {}
    for entry in value.lower().split(","):
        name, *parameters = entry.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        qualities[name.strip()] = quality
    return 0 < qualities.get("gzip", qualities.get("*", 0)) <= 1


class GuiRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    timeout = 30

    def setup(self):
        super().setup()
        self.connection_id, self.connection_started = self.server.status.connection_info(self.connection)

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.status.connection_closed(self.connection)

    def handle_one_request(self):
        self.request_id = None
        self.request_route = "<unparsed>"
        self.request_read_started = time.monotonic()
        self.request_started = self.request_read_started
        self.response_status = self.response_length = self.response_headers_at = None
        self.response_stream = False
        error = None
        try:
            super().handle_one_request()
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            if self.request_id:
                now = time.monotonic()
                self.server.status.record("HTTP_REQUEST_END", request=self.request_id, connection=self.connection_id,
                    route=getattr(self, "request_route", "<unparsed>"), status=self.response_status,
                    elapsed_ms=round((now-self.request_started)*1000, 3),
                    send_ms=round((now-self.response_headers_at)*1000, 3) if self.response_headers_at else None,
                    bytes=self.response_length, stream=self.response_stream, error=error)

    def parse_request(self):
        self.request_started = time.monotonic()
        self.request_id = secrets.token_hex(6)
        self.server.status.record("HTTP_REQUEST_START", request=self.request_id, connection=self.connection_id,
            idle_wait_ms=round((self.request_started-self.request_read_started)*1000, 3))
        result = super().parse_request()
        if result:
            self.request_route = self._safe_route()
            self.server.status.record("HTTP_HEADERS_READ", request=self.request_id,
                method=self.command if self.command in {"GET", "POST", "HEAD"} else "OTHER",
                route=self.request_route, header_read_ms=round((time.monotonic()-self.request_started)*1000, 3),
                page=self._safe_id(self.headers.get("X-recoding-Page")),
                operation=self._safe_id(self.headers.get("X-recoding-Operation")))
        return result

    @staticmethod
    def _safe_id(value):
        return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) else None

    def _safe_route(self, path=None):
        path = urllib.parse.urlsplit(self.path if path is None else path).path
        if path in self.server.assets or path in {"/api/config", "/api/sessions", "/api/progress", "/api/directories", "/api/diagnostics", "/api/workspace", "/api/gui/release", "/api/gui/takeover"}:
            return path
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
            route = "/api/sessions/{session}"
            if len(parts) >= 5 and parts[3] == "agents":
                route += "/agents/{agent}"
                parts = parts[4:]
            else:
                parts = parts[2:]
            if len(parts) > 1 and parts[1] in {"events", "images", "workspace-image", "tool-source", "history", "executions", "input", "cancel", "continue", "close", "metadata", "model", "agent-limit", "idle-sleep", "delete", "uploads"}:
                route += "/" + parts[1]
            return route
        return "<unmatched>"

    def log_message(self, format, *args):
        pass  # Prompts and session names never go to access logs.

    def _same_origin(self):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in allowed or (origin and origin != "http://" + host):
            raise PermissionError("Only same-origin localhost requests are allowed.")

    def _headers(self, status, content_type, length=None, content_encoding=None):
        self.response_status, self.response_length = status, length
        self.response_stream = content_type.startswith("text/event-stream")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept-Encoding")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        self.send_header("X-Agent-API-Version", str(API_VERSION))
        self.send_header("X-Agent-Instance", self.server.instance_id)
        self.send_header("X-recoding-Request", self.request_id)
        self.send_header("Server-Timing", f"app;dur={(time.monotonic()-self.request_started)*1000:.3f}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: blob: https: http:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()
        self.response_headers_at = time.monotonic()
        self.server.status.record("HTTP_RESPONSE_HEADERS", request=self.request_id,
            status=status, elapsed_ms=round((self.response_headers_at-self.request_started)*1000, 3),
            bytes=length, encoding=content_encoding)

    def _json(self, status, data):
        started = time.monotonic()
        encoded = json.dumps(data, ensure_ascii=False).encode()
        encoded_at = time.monotonic()
        raw_bytes = len(encoded)
        encoding = None
        if len(encoded) >= 4096 and accepts_gzip(self.headers.get("Accept-Encoding", "")):
            compressed = gzip.compress(encoded, compresslevel=3, mtime=0)
            if len(compressed) < len(encoded):
                encoded, encoding = compressed, "gzip"
        self.server.status.record("RESPONSE_PREPARED", request=self.request_id,
            serialize_ms=round((encoded_at-started)*1000, 3),
            compress_ms=round((time.monotonic()-encoded_at)*1000, 3), raw_bytes=raw_bytes, wire_bytes=len(encoded))
        self._headers(status, "application/json; charset=utf-8", len(encoded), encoding)
        self.wfile.write(encoded)

    def _not_found(self):
        self._json(404, {"error": "当前服务器不支持此接口，请重启服务器上的 Web 服务并刷新页面。",
                         "code": "route_not_found", "instance_id": self.server.instance_id})

    def _session_not_found(self, error):
        self._json(404, {"error": "当前服务器找不到该 Session，请刷新列表确认存档是否仍在此服务器。",
                         "code": "session_not_found", "detail": str(error),
                         "instance_id": self.server.instance_id})

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked request bodies are not accepted.")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            raise ValueError("Request body must be between 1 and 65536 bytes.")
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ValueError("Expected application/json.")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def _browser_diagnostics(self, body):
        if body.get("kind") not in {"startup", "open", "scan", "restart", "workspace"} or not re.fullmatch(r"[a-f0-9-]{36}", str(body.get("page", ""))):
            raise ValueError("Invalid browser diagnostics identity.")
        samples = body.get("samples", [])
        if not isinstance(samples, list) or len(samples) > 48:
            raise ValueError("Invalid browser timing sample count.")
        def duration(value):
            return round(value, 3) if type(value) in {float, int} and math.isfinite(value) and 0 <= value <= 86400000 else None
        metrics = []
        for sample in samples:
            if not isinstance(sample, dict) or not isinstance(sample.get("path"), str):
                raise ValueError("Invalid browser timing sample.")
            metrics.append({"route": self._safe_route(sample["path"]),
                **{key: duration(sample.get(key)) for key in ("start_ms", "duration_ms", "queue_connect_ms", "dns_ms", "connect_ms", "ttfb_ms", "transfer_ms", "server_ms")}})
        self.server.status.record("BROWSER_TIMING", page=body["page"], kind=body["kind"],
            elapsed_ms=duration(body.get("elapsed_ms")), samples=metrics,
            operations=[value for value in body.get("operations", [])[:8]
                        if isinstance(value, str) and re.fullmatch(r"[a-f0-9-]{36}", value)] if isinstance(body.get("operations", []), list) else [])

    def _record_operation(self, progress):
        value = progress.snapshot()
        def stages(data):
            return [{"stage": step["key"], "elapsed_ms": round(step["elapsed_seconds"]*1000, 3),
                     "status": step["status"]} for step in data.get("steps", [])]
        self.server.status.record("SESSION_OPERATION", request=self.request_id,
            method=self.command, status=value["status"], elapsed_ms=round(value["elapsed_seconds"]*1000, 3),
            stages=stages(value), runtime_stages=stages(value.get("runtime", {})))

    def _gui_identity(self):
        parsed=urllib.parse.urlsplit(self.path)
        query=urllib.parse.parse_qs(parsed.query)
        return (self.headers.get('X-recoding-Page') or query.get('page',[''])[0],
                self.headers.get('X-recoding-Document') or query.get('document',[None])[0])

    def _gui_busy(self, error):
        # Same-origin bootstrap for the blocked page's explicit takeover button.
        self._json(409,{'error':str(error),'code':'gui_busy',
                        'takeover_token':self.server.token,'instance_id':self.server.instance_id})

    def _admit_gui(self):
        parsed=urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith('/api/'):return
        with self.server.gui_lock:
            previous=self.server.host.connect_gui(*self._gui_identity())
            if previous:self.server.streams.close_page(previous,'GUI connection replaced')

    def _gui_control(self, path):
        body=self._body()
        release=path=='/api/gui/release'
        required={'instance_id','page','document','csrf'} if release else {'instance_id'}
        if set(body)!=required:raise ValueError('Invalid GUI control fields')
        token=body.get('csrf') if release else self.headers.get('X-Agent-Token','')
        if not isinstance(token,str) or not secrets.compare_digest(token,self.server.token):
            raise PermissionError('Missing or invalid GUI request token. Reload this page.')
        if body['instance_id']!=self.server.instance_id:raise ValueError('Web 服务已重启，请刷新页面')
        with self.server.gui_lock:
            if release:
                released=self.server.host.release_gui(body['page'],body['document'])
                if released:self.server.streams.close_page(body['page'],'GUI page closed')
                self.server.status.record('GUI_RELEASE',page=self._safe_id(body['page']),released=released)
                result={'released':released}
            else:
                page,document=self._gui_identity()
                if not document:raise ValueError('A document identity is required for takeover')
                previous=self.server.host.connect_gui(page,document,takeover=True)
                if previous:self.server.streams.close_page(previous,'GUI explicitly taken over')
                self.server.status.record('GUI_TAKEOVER',page=self._safe_id(page),previous=self._safe_id(previous))
                result={'connected':True,'instance_id':self.server.instance_id}
        self._json(200,result)

    def do_GET(self):
        try:
            self._same_origin()
            self._admit_gui()
            with self.server.host.read_request(urllib.parse.urlsplit(self.path).path):
                self._get()
        except GuiBusyError as exc:self._gui_busy(exc)
        except PermissionError as exc:self._json(403,{'error':str(exc),'code':'session_busy'})
        except ValueError as exc:self._json(409,{'error':str(exc),'code':'session_busy'})
        except KeyError as exc:self._session_not_found(exc)

    def _get(self):
        try:
            self._same_origin()
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path in self.server.assets:
                data, mime = self.server.assets[path]
                encoding = None
                if path in self.server.compressed_assets and accepts_gzip(self.headers.get("Accept-Encoding", "")):
                    data, encoding = self.server.compressed_assets[path], "gzip"
                self._headers(200, mime, len(data), encoding)
                self.wfile.write(data)
                if path == "/":
                    self.server.status.page_loaded(self.client_address[0])
                return
            if path == "/health":
                self._json(200,{"status":"listening","service":"recoding-web"})
                return
            if path == "/api/heartbeat":
                self._json(200,{"connected":True})
                return
            if path == "/api/config":
                self._json(200, self.server.configuration())
                return
            if path == "/api/progress":
                query = urllib.parse.parse_qs(parsed.query)
                self._json(200, {"operation": self.server.progress.get(query.get("id", [""])[0])})
                return
            if path == "/api/sessions":
                progress = self.server.progress.begin(self.headers.get("X-recoding-Operation"))
                try:
                    query = urllib.parse.parse_qs(parsed.query)
                    expanded = json.loads(query.get("expanded", ["[]"])[0])
                    data = self.server.host.catalog.snapshot(progress, expanded=expanded)
                    progress.stage("response", "编码并发送 Session 列表")
                    self._json(200, data)
                except Exception as error:
                    progress.finish(error)
                    raise
                else:
                    progress.finish()
                    self._record_operation(progress)
                return
            if path == "/api/directories":
                query = urllib.parse.parse_qs(parsed.query)
                self._json(200, self.server.host.catalog.directories(query.get("path", [None])[0]))
                return
            parts = path.strip("/").split("/")
            if len(parts) >= 5 and parts[:2] == ["api", "sessions"] and parts[3] == "agents":
                identifier, agent_id = urllib.parse.unquote(parts[2]), parts[4]
                if len(parts) == 5:
                    self._json(200, self.server.host.agent_snapshot(identifier, agent_id))
                    return
                session, store, _ = self.server.host.agent_store(identifier, agent_id)
                query = urllib.parse.parse_qs(parsed.query)
                if len(parts) == 6 and parts[5] == "executions":
                    self._json(200, session.list_executions(agent_id))
                    return
                if len(parts) == 8 and parts[5] == "executions" and parts[7] == "output":
                    self._json(200, session.execution_output(agent_id, parts[6],
                        stdout_offset=int(query.get("stdout_offset", ["0"])[0]),
                        stderr_offset=int(query.get("stderr_offset", ["0"])[0]),
                        max_bytes=int(query.get("max_bytes", ["16000"])[0])))
                    return
                if len(parts) == 6 and parts[5] == "history":
                    events = [preview(e) for e in store.load_events(400, int(query.get("before", [str(2**63)])[0]))]
                    self._json(200, {"events": events, "has_more": bool(events and events[0]["sequence"] > 1)})
                    return
                if len(parts) == 6 and parts[5] == "tool-source":
                    self._json(200, store.tool_source(query.get("run_id", [""])[0], query.get("call_id", [""])[0], int(query.get("sequence", ["0"])[0])))
                    return
                if len(parts) == 7 and parts[5] == "images":
                    data, mime = store.image_asset(parts[6])
                    self._headers(200, mime, len(data));self.wfile.write(data)
                    return
                if len(parts) == 6 and parts[5] == "workspace-image":
                    data, mime = AgentMedia(store, self.server.config["private_files"], [session.store.folder.parent]).bytes(query.get("path", [""])[0])
                    self._headers(200, mime, len(data));self.wfile.write(data)
                    return
                self._not_found()
                return
            if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "images":
                data, mime = self.server.host.image(urllib.parse.unquote(parts[2]), digest=parts[4])
                self._headers(200, mime, len(data))
                self.wfile.write(data)
                return
            if len(parts) in {3, 4} and parts[:2] == ["api", "sessions"]:
                if len(parts) == 3:
                    self._json(200, self.server.host.inspect(urllib.parse.unquote(parts[2])))
                elif parts[3] == "events":
                    query = urllib.parse.parse_qs(parsed.query)
                    if query.get("instance", [self.server.instance_id])[0] != self.server.instance_id:
                        self._headers(204, "text/event-stream", 0)
                        return  # Tell EventSource to stop reconnecting to a replaced server.
                    try:
                        session = self.server.host.get(urllib.parse.unquote(parts[2]))
                    except KeyError:
                        self._headers(204, "text/event-stream", 0)
                        return
                    if isinstance(session,SavedSession):
                        self._headers(204,'text/event-stream',0)
                        return
                    after = int(self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0])
                    self._events(session, after, query.get("page", [None])[0],query.get('document',[None])[0])
                elif parts[3] == "history":
                    query = urllib.parse.parse_qs(parsed.query)
                    before = int(query.get("before", [str(2**63)])[0])
                    self._json(200, self.server.host.history_page(urllib.parse.unquote(parts[2]), before))
                elif parts[3] == 'python-pool':
                    self._json(200,self.server.host.get(urllib.parse.unquote(parts[2])).python_resources())
                elif parts[3] == "messages":
                    query = urllib.parse.parse_qs(parsed.query)
                    session = self.server.host.get(urllib.parse.unquote(parts[2]))
                    self._json(200, session.messages(before=query.get("before", [None])[0],
                        after=query.get("after", [None])[0], limit=int(query.get("limit", ["100"])[0])))
                elif parts[3] == "tool-source":
                    query = urllib.parse.parse_qs(parsed.query)
                    self._json(200, self.server.host.tool_source(urllib.parse.unquote(parts[2]),
                        query.get("run_id", [""])[0], query.get("call_id", [""])[0],
                        int(query.get("sequence", ["0"])[0])))
                elif parts[3] == "workspace-image":
                    query = urllib.parse.parse_qs(parsed.query)
                    image_path = query.get("path", [""])[0]
                    data, mime = self.server.host.image(urllib.parse.unquote(parts[2]), path=image_path)
                    self._headers(200, mime, len(data))
                    self.wfile.write(data)
                else:
                    self._not_found()
                return
            self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except GuiBusyError:
            raise
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except KeyError as exc:
            self._session_not_found(exc)
        except UnsupportedSession as exc:
            self.close_connection = True
            self._json(400, {"error": str(exc), "code": "unsupported_session"})
        except (ValueError, OSError) as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self):
        try:
            self._same_origin()
            path = urllib.parse.urlsplit(self.path).path
            if path in {'/api/gui/release','/api/gui/takeover'}:
                self._gui_control(path)
                return
            if not secrets.compare_digest(self.headers.get("X-Agent-Token", ""), self.server.token):
                raise PermissionError("Missing or invalid request token. Reload this page.")
            self._admit_gui()
            parts = path.strip("/").split("/")
            upload_target = (len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "uploads")
            child_upload = (len(parts) == 6 and parts[:2] == ["api", "sessions"] and parts[3] == "agents" and parts[5] == "uploads")
            if upload_target or child_upload:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Chunked uploads are not accepted.")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_IMAGE_BYTES:
                    raise ValueError("图片必须为 PNG/JPEG，且不超过 5 MiB。")
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError("图片上传未完成。")
                self.server.host.require_gui(*self._gui_identity())
                picture = self.server.host.upload(urllib.parse.unquote(parts[2]), data,
                    urllib.parse.unquote(self.headers.get("X-File-Name", "image")), parts[4] if child_upload else "master")
                self._json(201, picture)
                return
            body = self._body()
            self.server.host.require_gui(*self._gui_identity())
            if path == '/api/exit':
                self._json(202,{'closing':True})
                import threading
                threading.Thread(target=self.server.shutdown,daemon=True).start()
                return
            if path == "/api/workspace":
                progress = self.server.progress.begin(self.headers.get("X-recoding-Operation"))
                try:
                    result = self.server.host.catalog.change_workspace(body.get("directory"), progress)
                    self._json(200, result)
                except Exception as error:
                    progress.finish(error)
                    raise
                else:
                    progress.finish()
                    self._record_operation(progress)
                return
            if path == "/api/diagnostics":
                self._browser_diagnostics(body)
                self._json(202, {"recorded": True})
                return
            if path == "/api/sessions":
                if any(type(body.get(key, True)) is not bool for key in ("network", "gpu")):
                    raise ValueError("network and gpu must be booleans.")
                if body.get("action") not in {None, "create", "resume", "browse"}:
                    raise ValueError("未知 Session 操作。")
                for key in ("directory", "path", "id"):
                    if key in body and (not isinstance(body[key], str) or not body[key].strip()):
                        raise ValueError("Session 路径和 ID 必须是非空字符串。")
                progress = self.server.progress.begin(self.headers.get("X-recoding-Operation"))
                try:
                    session = self.server.host.open(body.get("name"), action=body.get("action"),
                        directory=body.get("directory"), path=body.get("path"), identifier=body.get("id"),
                        network=body.get("network", True), gpu=body.get("gpu", True), model=body.get("model"), execution_mode=body.get("execution_mode"),
                        max_subagents=body.get("max_subagents", 16), progress=progress)
                    progress.stage("snapshot", "整理 Session 页面数据", session.name)
                    data = session.snapshot()
                    progress.stage("response", "编码并发送 Session 历史")
                    self._json(201, data)
                except Exception as error:
                    progress.finish(error)
                    raise
                else:
                    progress.finish()
                    self._record_operation(progress)
                return
            parts = path.strip("/").split("/")
            if len(parts) == 6 and parts[:2] == ["api", "sessions"] and parts[3] == "agents" and parts[5] == "input":
                self._json(202, self.server.host.submit(urllib.parse.unquote(parts[2]), parts[4], body.get("prompt"), body.get("attachments", [])))
                return
            if len(parts) == 6 and parts[:2] == ["api", "sessions"] and parts[3] == "agents" and parts[5] == "tool-inventory":
                session = self.server.host.running(urllib.parse.unquote(parts[2]))
                self._json(202, session.inspect_tools(parts[4]))
                return
            if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                name, action = urllib.parse.unquote(parts[2]), parts[3]
                if action == "metadata":
                    self._json(200, self.server.host.update(name, body))
                    return
                if action == "messages":
                    if set(body) - {"prompt", "attachments", "recipient"}:
                        raise ValueError("消息只接受正文、图片和接收者。")
                    session = self.server.host.running(name)
                    self._json(202, session.submit_message(body.get("prompt"), body.get("attachments", []),
                                                         recipient=body.get("recipient", "master")))
                    return
                if action == "nodes":
                    self._json(200,self.server.host.nodes(name,body))
                    return
                if action == "model":
                    self._json(200, self.server.host.set_model(name, body))
                    return
                if action == "agent-limit":
                    self._json(200, self.server.host.set_agent_limit(name, body))
                    return
                if action == "idle-sleep":
                    self._json(200, self.server.host.set_idle_sleep(name, body))
                    return
                if action == "delete":
                    self._json(200, self.server.host.delete(name))
                    return
                if action == 'continue':
                    self._json(202,self.server.host.continue_task(name,body.get('settings')))
                    return
                session = self.server.host.running(name)
                if action == "tool-inventory":
                    self._json(202, session.inspect_tools())
                elif action == "context-compaction":
                    self._json(202, session.compaction.cancel_request(body))
                elif action == "input":
                    self._json(202, self.server.host.submit(name, "master", body.get("prompt"), body.get("attachments", [])))
                elif action == "cancel":
                    self.server.host.close_session(name)
                    self._json(200, {"accepted": True,"status":"stop"})
                elif action == "close":
                    self.server.host.close_session(name)
                    self._json(200, {"closed": True})
                else:
                    self._not_found()
                return
            self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except SessionBusyError as exc:
            self.close_connection=True
            self._json(409,{'error':str(exc),'code':'session_busy'})
        except GuiBusyError as exc:
            self.close_connection=True
            self._gui_busy(exc)
        except PermissionError as exc:
            self.close_connection = True
            self._json(403, {"error": str(exc)})
        except KeyError as exc:
            self._session_not_found(exc)
        except UnsupportedSession as exc:
            self.close_connection = True
            self._json(400, {"error": str(exc), "code": "unsupported_session"})
        except (ValueError, OSError) as exc:
            self.close_connection = True
            self._json(400, {"error": str(exc)})

    def _events(self, session, after, page=None, document=None):
        if not isinstance(page, str) or not re.fullmatch(r"[a-f0-9-]{36}", page):
            raise ValueError("A browser page ID is required for event subscriptions")
        self.close_connection = True
        with self.server.gui_lock:
            self.server.host.require_gui(page,document)
            subscription = self.server.streams.open(page, session.id, self.client_address[0], self.connection)
        reason = "session closed"
        try:
            self._headers(200, "text/event-stream; charset=utf-8")
            self._stream_events(session, after, subscription)
        except (BrokenPipeError, ConnectionResetError):
            reason = "browser disconnected"
            raise
        except OSError:
            reason = "connection error"
            raise
        finally:
            self.server.streams.close(subscription, reason)

    def _stream_events(self, session, after, subscription):
        while not subscription.cancelled.is_set():
            with session.condition:
                resync = bool(session.events and after < session.events[0]["sequence"] - 1)
                events = [event for event in session.events if event["sequence"] > after]
                if not events and not session.closed and not subscription.cancelled.is_set():
                    session.condition.wait(2)
                    events = [event for event in session.events if event["sequence"] > after]
                # The page follows the Session across Stop/Resume. Individual
                # Agent workers are expected to exit when stopped.
                closed = session.closed
            if subscription.cancelled.is_set():
                break
            if resync:
                snapshot = session.snapshot()
                events = [{"type": "resync", "snapshot": snapshot, "sequence": snapshot["sequence"]}]
            if events:
                for event in events:
                    after = event["sequence"]
                    packet = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"id: {after}\ndata: {packet}\n\n".encode())
            else:
                self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
            if closed:
                if session.master.lease.fd is not None:
                    time.sleep(.01)
                    continue
                packet=json.dumps({'type':'session_stopped','status':'stop','sequence':after})
                self.wfile.write(f'data: {packet}\n\n'.encode());self.wfile.flush()
                break


class GuiHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    # Reconnecting old tabs and a fresh page's parallel assets can exceed the
    # Python 3.10 default of five, causing SYN retransmission waits before HTTP.
    request_queue_size = 128

    def __init__(self, address, host):
        self.host=host
        self.config=host.config
        self.token=secrets.token_urlsafe(32)
        self.instance_id=secrets.token_hex(8)
        self.gui_lock=threading.Lock()
        self.progress=ProgressRegistry()
        self.assets={path:((STATIC/filename).read_bytes(),mime) for path,(filename,mime) in ASSETS.items()}
        self.compressed_assets={path:gzip.compress(data,compresslevel=3,mtime=0)
            for path,(data,mime) in self.assets.items() if len(data)>=4096 and mime.startswith(('text/','application/javascript'))}
        revision=hashlib.sha256()
        for path,(data,_) in sorted(self.assets.items()):
            revision.update(path.encode());revision.update(hashlib.sha256(data).digest())
        self.frontend_revision=revision.hexdigest()[:16]
        self.status=self.streams=None
        super().__init__(address,GuiRequestHandler)
        try:
            self.status=WebStatus(host.catalog.root,self.instance_id)
            self.streams=PageStreams(self.status)
        except BaseException:
            super().server_close()
            raise
        self.status.listen(self.server_address,self.instance_id,host.catalog.root)
        self.status.record('SERVER_CONFIG',frontend_revision=self.frontend_revision,runtime_revision=RUNTIME_REVISION,
            startup_ms=host.startup_ms,**{key:host.config[key] for key in ('demo','network','gpu')})

    def configuration(self):
        return {"csrf": self.token, "demo": self.config["demo"],
                "service": "recoding-web", "pid": os.getpid(),
                "product_name": "recoding", "short_name": "recoding", "version": "0.8.0",
                "api_version": API_VERSION, "instance_id": self.instance_id,
                "frontend_revision": self.frontend_revision, "runtime_revision": RUNTIME_REVISION,
                "capabilities": dict(CAPABILITIES),
                "model": "offline-demo" if self.config["demo"] else default_model(),
                "models": list(model_choices()),
                "model_profiles": public_profiles(model_choices()),
                "model_style": {"unknown_colors":list(UNKNOWN_COLORS)},
                "key_configured": bool(api_key()),
                "session_root": str(self.host.catalog.root),
                "defaults": {key: value for key, value in self.config.items() if key != "private_files"}}

    def get_request(self):
        connection, address = super().get_request()
        self.status.accepted(connection, address[0])
        return connection, address

    def server_close(self):
        if self.streams:
            self.streams.close_all()
        super().server_close()
        if self.status:
            self.status.stopped()


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local Web GUI for recoding")
    parser.add_argument("workspace", nargs="?", help="initial workspace and recursive Session search root")
    parser.add_argument("-C", "--directory", help="initial workspace and recursive Session search root (default: current directory)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-replace", action="store_true", help="fail if the port is occupied; do not replace an existing recoding Web server")
    parser.add_argument("--demo", action="store_true", help="labelled offline scripted responses with real sandbox tools; no API key")
    parser.add_argument("--execution-mode",choices=["sandbox","host"],default="sandbox",help="default for new Sessions; host runs tools without OS isolation")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--tool-timeout", type=positive, default=30)
    parser.add_argument("--api-timeout", type=positive, default=120)
    parser.add_argument("--env-file", help="local model configuration file (default: project .env)")
    args = parser.parse_args(argv)
    from multiagent.config import validate_execution_config
    try:validate_execution_config({"execution_mode":args.execution_mode,"network":not args.no_network,"gpu":not args.no_gpu})
    except ValueError as error:parser.error(str(error))
    if args.workspace and args.directory:
        parser.error("specify the startup directory either as a positional path or with -C")
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    host = server = None
    previous_term = None
    try:
        env_file = Path(args.env_file) if args.env_file else default_env_file()
        if not args.demo:
            load_env_file(env_file)
        host = GuiSessionHost(args.directory or args.workspace or ".", execution_mode=args.execution_mode,demo=args.demo, network=not args.no_network, gpu=not args.no_gpu,
                          tool_timeout=args.tool_timeout, api_timeout=args.api_timeout, private_files=[env_file], defer_discovery=True)
        try:
            server = GuiHttpServer(("127.0.0.1", args.port), host)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or args.no_replace or args.port == 0:
                raise
            replace_listener(args.port)
            server = GuiHttpServer(("127.0.0.1", args.port), host)
        # First discovery runs in an HTTP worker, after the old runtime exits.
        # Serve HTML/config immediately so the browser can show real scan status.
        def terminate(signum, frame):
            raise KeyboardInterrupt()
        previous_term = signal.signal(signal.SIGTERM, terminate)
        print(f"Web GUI: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Server PID: {os.getpid()} · Instance: {server.instance_id} · UI: {server.frontend_revision}", flush=True)
        print("Mode: " + ("DEMO · fixed responses, real tools, no model API" if args.demo else "Model API · configured endpoint"), flush=True)
        if not args.demo and not api_key():
            print("RECODING_API_KEY 未设置；请填写项目 .env 或设置环境变量后重新启动。", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        if host is not None:
            host.close()
        if server is not None:
            server.server_close()
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
    return 0
