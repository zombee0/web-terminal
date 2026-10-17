"""Safe, temporary ingress probe; no terminal or control actions are exposed."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        health = self.path == "/healthz"
        self.send_response(200 if health else 503)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"ok\n" if health else b"Deployment in progress.\n")

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 3001), Handler).serve_forever()
