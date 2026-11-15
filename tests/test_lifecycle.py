"""The real bot process against a fake Bot API server: start, heartbeat, SIGTERM.

Offline: the "Telegram" here is a local HTTP server that answers every method.
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

TOKEN = "123456:" + "A" * 35
ME = {
    "id": 123456,
    "is_bot": True,
    "first_name": "Test",
    "username": "test_bot",
    "can_join_groups": True,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}


@pytest.fixture
def bot_api() -> Iterator[tuple[str, list[str]]]:
    calls: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            method = self.path.rsplit("/", 1)[-1]
            calls.append(method)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if method == "getUpdates":
                time.sleep(0.2)
                result: object = []
            elif method == "getMe":
                result = ME
            else:
                result = True
            body = json.dumps({"ok": True, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    class Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address) -> None:
            pass  # the bot closing its long poll at shutdown is expected

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/bot", calls
    server.shutdown()
    server.server_close()


def test_bot_starts_beats_and_stops_cleanly_on_sigterm(bot_api, tmp_path: Path) -> None:
    base_url, calls = bot_api
    heartbeat = tmp_path / "heartbeat"
    work = tmp_path / "tmp"
    work.mkdir()
    stale = work / "sbahelper-dl-old"  # left behind by a "crash"
    stale.mkdir()
    (stale / ".sbahelper-workdir").write_text("1\n")
    env = {
        **os.environ,
        "DOWNLOADER_BOT_TOKEN": TOKEN,
        "ADMIN_IDS": "",
        "STATS_DB_PATH": str(tmp_path / "data" / "bot.db"),
        "COOKIES_DIR": str(tmp_path / "data" / "cookies"),
        "TEMP_DIR": str(work),
        "HEARTBEAT_PATH": str(heartbeat),
        "CHECK_TIKTOK_URL": "",
        "CHECK_YOUTUBE_URL": "",
        "NO_COLOR": "1",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from sbahelper import app; sys.exit(app.main(sys.argv[1]))",
            base_url,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not heartbeat.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.1)
        if not heartbeat.exists():
            process.kill()
            pytest.fail("bot never wrote its heartbeat:\n" + process.communicate()[0])
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=40)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0, output
    assert "Bot started." in output and "Download service stopped." in output
    assert "Bot stopped." in output
    assert not heartbeat.exists()  # removed on shutdown
    assert not stale.exists()  # abandoned work dir cleaned at startup
    assert {"getMe", "getUpdates", "setMyCommands"} <= set(calls)
    assert TOKEN not in output and "AAAAAAAAAA" not in output
    assert (tmp_path / "data" / "bot.db").stat().st_mode & 0o077 == 0
