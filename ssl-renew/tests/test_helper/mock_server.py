#!/usr/bin/env python3
"""Local HTTP(S) mock server for ssl-renew bats/pytest tests.

Serves a fixed status code + body to every GET/POST/PUT/DELETE (with
optional per-method overrides — see --post-status etc. — so a single
server instance can model e.g. "upload succeeds, bind fails"), optionally
over TLS with a caller-supplied cert/key, optionally with an artificial
delay (to exercise client-side timeouts). Never talks to anything outside
127.0.0.1. Used by notify.sh webhook tests, verify_https.sh HTTPS tests,
and qiniu_helper.py tests (upload=POST, bind=PUT, verify=GET).
"""
import argparse
import http.server
import ssl
import sys
import time

ARGS = None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self, method):
        if ARGS.delay:
            time.sleep(ARGS.delay)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        if ARGS.log_file:
            with open(ARGS.log_file, "ab") as f:
                f.write(method.encode("ascii") + b" " + body + b"\n")

        status = getattr(ARGS, f"{method.lower()}_status", None) or ARGS.status
        text_body = getattr(ARGS, f"{method.lower()}_body", None) or ARGS.body
        response_body = text_body.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        if ARGS.reqid:
            # The official qiniu SDK's ResponseInfo treats a 200 response
            # with no X-Reqid header as "not really Qiniu" and discards
            # the body — real Qiniu responses always carry this header,
            # so the mock must too or every 200-status test would look
            # like a protocol error instead of exercising the real path.
            self.send_header("X-Reqid", ARGS.reqid)
        self.end_headers()
        self.wfile.write(response_body)

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def log_message(self, fmt, *args):
        pass


def main():
    global ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--status", type=int, default=200)
    p.add_argument("--body", default="{}")
    # Per-method overrides — fall back to --status/--body when unset. Lets
    # one server instance model a multi-step chain (e.g. upload=POST
    # succeeds while bind=PUT fails) without juggling multiple ports.
    p.add_argument("--post-status", type=int, default=None)
    p.add_argument("--post-body", default=None)
    p.add_argument("--put-status", type=int, default=None)
    p.add_argument("--put-body", default=None)
    p.add_argument("--get-status", type=int, default=None)
    p.add_argument("--get-body", default=None)
    p.add_argument("--reqid", default="fake-test-reqid", help="X-Reqid header value; pass '' to omit it")
    p.add_argument("--delay", type=float, default=0)
    p.add_argument("--tls", action="store_true")
    p.add_argument("--cert")
    p.add_argument("--key")
    p.add_argument("--log-file")
    p.add_argument("--ready-file")
    ARGS = p.parse_args()

    server = http.server.HTTPServer(("127.0.0.1", ARGS.port), Handler)
    if ARGS.tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=ARGS.cert, keyfile=ARGS.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    if ARGS.ready_file:
        with open(ARGS.ready_file, "w") as f:
            f.write("ready\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
