"""Test-process configuration only; actual HTTP app, models and tool handlers stay real."""

import json
import os
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tests.e2e.suite import ROOT


def main() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import get_settings

    from tests.e2e.isolated import isolated_url

    settings = get_settings()
    settings.database_url = isolated_url(settings.database_url, os.environ["E2E_DATABASE"])
    settings.agent_runtime = "strands"
    settings.loop_trace_enabled = True
    folder = Path(os.environ["E2E_PROFILE_DIR"])
    # The production service has constructor injection but no Settings path fields.
    # Bind only its input files before contracts imports the factory; no result stubs.
    from app.tools import knowledge

    knowledge.KnowledgeRetrievalToolService = partial(
        knowledge.KnowledgeRetrievalToolService,
        document_path=folder / "policy.json",
        vector_index_path=folder / "absent-vector-index.json",
    )
    if os.environ["E2E_PROFILE"] in {"web_unavailable", "empty_web"}:
        import app.tools.web_search as web
        from pydantic import SecretStr

        class Unavailable(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                empty = os.environ["E2E_PROFILE"] == "empty_web"
                status = 200 if empty else 503
                with (folder / "search-fault.jsonl").open("a") as stream:
                    stream.write(json.dumps({"path": self.path, "status": status,
                                             "results": [] if empty else None}) + "\n")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if empty:
                    self.wfile.write(b'{"results": []}')

            def log_message(self, *args):
                pass

        fault = HTTPServer(("127.0.0.1", 0), Unavailable)
        threading.Thread(target=fault.serve_forever, daemon=True).start()
        web.DEEPSEEK_SEARCH_URL = f"http://127.0.0.1:{fault.server_port}/search"
        settings.deepseek_search_api_key = SecretStr("synthetic-e2e-no-secret")

    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=int(os.environ["E2E_PORT"]))


if __name__ == "__main__":
    main()
