import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
SHARED_SECRET = os.environ.get("RELAY_SHARED_SECRET", "").strip()
# Render sets PORT; local dev can use RELAY_PORT.
PORT = int(os.environ.get("PORT", os.environ.get("RELAY_PORT", "8787")))


def discord_headers():
    return {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "fenti-corpse-relay/1.0",
    }


def discord_post_message(payload):
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN is not set")
    if not CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID is not set")

    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages"
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=discord_headers(),
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "FentiCorpseRelay/1.0"

    def _reply(self, status_code, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self._reply(
                200,
                {
                    "ok": True,
                    "bot_configured": bool(BOT_TOKEN),
                    "channel_configured": bool(CHANNEL_ID),
                },
            )
            return
        self._reply(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path != "/api/corpse-log":
            self._reply(404, {"ok": False, "error": "not_found"})
            return

        if SHARED_SECRET:
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {SHARED_SECRET}"
            if auth != expected:
                self._reply(401, {"ok": False, "error": "unauthorized"})
                return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"ok": False, "error": "bad_content_length"})
            return

        raw = self.rfile.read(length)
        try:
            incoming = json.loads(raw.decode("utf-8"))
        except Exception:
            self._reply(400, {"ok": False, "error": "bad_json"})
            return

        if not isinstance(incoming, dict):
            self._reply(400, {"ok": False, "error": "json_object_required"})
            return

        outgoing = {
            "username": str(incoming.get("username") or "fenti corpse sniper")[:80],
            "avatar_url": incoming.get("avatar_url"),
            "embeds": incoming.get("embeds") if isinstance(incoming.get("embeds"), list) else [],
            "allowed_mentions": {"parse": []},
        }

        if not outgoing["embeds"]:
            self._reply(400, {"ok": False, "error": "missing_embeds"})
            return

        try:
            discord_post_message(outgoing)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._reply(
                502,
                {"ok": False, "error": "discord_http_error", "status": exc.code, "detail": detail},
            )
            return
        except URLError as exc:
            self._reply(502, {"ok": False, "error": "discord_network_error", "detail": str(exc)})
            return
        except Exception as exc:
            self._reply(500, {"ok": False, "error": "relay_error", "detail": str(exc)})
            return

        self._reply(200, {"ok": True})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RelayHandler)
    print(f"Listening on 0.0.0.0:{PORT}")
    server.serve_forever()
