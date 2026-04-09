#!/usr/bin/env python3
"""Minimal HTTP file server with CORS headers for serving Perfetto traces.

Used by capture_screenshots.py URL deep-link mode so that
https://ui.perfetto.dev can fetch a trace from the local filesystem via:

    https://ui.perfetto.dev/#!/?url=http://127.0.0.1:{port}/{filename}

Chrome treats http://127.0.0.1 as a secure context (since M94) so the
mixed-content rule is not triggered. The permissive CORS headers let
the Perfetto UI origin actually fetch the trace.

Inspired by Mambo's file_server approach; rewritten from scratch (the
Mambo source was only available as photos and was not transcribed).
"""
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class CORSHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with permissive CORS headers."""

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "public, max-age=3600")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        # Request-level logging for debugging. Quiet later once URL
        # deep-link loading path is validated.
        super().log_message(format, *args)


def start_file_server(serve_dir, port=9002):
    """Start a background HTTP server serving `serve_dir` on `port`.

    Returns the ThreadingHTTPServer — call stop_file_server(server) to shut down.
    """
    serve_dir = Path(serve_dir).resolve()
    handler_cls = lambda *args, **kw: CORSHandler(
        *args, directory=str(serve_dir), **kw
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def stop_file_server(server):
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    serve_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9002
    print(f"Serving {serve_dir} on http://127.0.0.1:{port}/")
    s = start_file_server(serve_dir, port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        stop_file_server(s)
