"""Loopback HTTPS/SSE model fixture; never contacts an external model API."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import subprocess
import threading
from types import SimpleNamespace


@contextmanager
def local_model(directory, respond):
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    key, cert = folder / "key.pem", folder / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
                    "-keyout", str(key), "-out", str(cert)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fixture = SimpleNamespace(requests=[], errors=[], cert=cert)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(body)
                message = respond(body, len(fixture.requests))
                result = {"choices": [{"index": 0, "delta": message,
                                       "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                          "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
                data = ("data: " + json.dumps(result) + "\n\ndata: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            except Exception as error:
                fixture.errors.append(str(error))
                data = json.dumps({"error": str(error)}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    fixture.url = f"https://127.0.0.1:{server.server_port}/v1"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
