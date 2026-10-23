#!/usr/bin/env python3
"""Lightweight node agent stub for testing/wiring audit.

Provides a minimal /health endpoint and a couple of safe responses so the
gateway's probes and the wiring audit can mark the node as reachable.
"""
import argparse
import json
from http.server import HTTPServer, BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        # respond to HEAD consistently so hub probes succeed
        if self.path.startswith('/health'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            return

        if self.path in ('/', '/v1/models'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith('/health'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'node': 'ok', 'name': 'node1'}).encode())
            return

        if self.path in ('/', '/v1/models'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'models': ['node1']}).encode())
            return

        # default
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # keep console output concise for test runs
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8095)
    args = p.parse_args()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"Node agent stub listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
