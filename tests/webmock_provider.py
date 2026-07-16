"""Loopback mock OpenRouter server for web-worker integration tests (M9).

A stdlib http.server bound to 127.0.0.1 that answers POST /chat/completions
with a canned, valid completion envelope. Used ONLY by tests that spawn the
real worker subprocess (cancellation/e2e): the WORKER process points
OPENROUTER_BASE_URL at this server, so the full engine runs with zero
external network access and zero cost. The TEST process only binds/accepts -
it never connects - so the autouse network-block fixture is unaffected.

`delay_after` makes every request AFTER the Nth block for `delay_seconds`,
giving cancellation tests a deterministic in-flight request to interrupt.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _envelope(content: str) -> dict:
    return {
        "id": "gen-mock", "model": "mock/served",
        "choices": [{"finish_reason": "stop", "native_finish_reason": "STOP",
                     "message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                  "total_tokens": 150, "cost": 0.0001,
                  "completion_tokens_details": {"reasoning_tokens": 0}},
    }


INVOICE_JSON = json.dumps({
    "invoice_number": "INV-1", "invoice_date": "2026-07-01", "currency": "EUR",
    "seller_name": "Mock Seller", "total_amount": 100.0,
    "line_items": [{"description": "Mock line", "quantity": 1,
                    "unit_price": 100.0, "amount": 100.0}],
})


class MockProvider:
    def __init__(self, *, delay_after: int = 10**9, delay_seconds: float = 8.0):
        self.request_count = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                with outer._lock:
                    outer.request_count += 1
                    n = outer.request_count
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if n > delay_after:
                    time.sleep(delay_seconds)
                body = json.dumps(_envelope(INVOICE_JSON)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence request logging
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "MockProvider":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        self._server.shutdown()
        self._server.server_close()
        return False
