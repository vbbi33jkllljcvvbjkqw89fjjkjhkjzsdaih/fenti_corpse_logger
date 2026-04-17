import io
import json
import os
import time
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
    body_bytes = json.dumps(payload).encode("utf-8")
    hdrs = discord_headers()

    for attempt in range(1, 5):
        req = Request(url, data=body_bytes, headers=hdrs, method="POST")
        try:
            with urlopen(req, timeout=25) as resp:
                out = resp.read().decode("utf-8", errors="replace")
                sc = getattr(resp, "status", None) or getattr(resp, "code", None) or "?"
                print(f"[relay] Discord POST ok http_status={sc} response_len={len(out)}")
                return out
        except HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            print(f"[relay] Discord HTTPError {exc.code} attempt={attempt} body={err_body[:400]}")
            if exc.code == 429 and attempt < 4:
                wait_sec = 2.0
                try:
                    j = json.loads(err_body)
                    if isinstance(j, dict) and isinstance(j.get("retry_after"), (int, float)):
                        wait_sec = float(j["retry_after"]) + 0.6
                except Exception:
                    pass
                try:
                    if exc.headers:
                        ra = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
                        if ra:
                            wait_sec = max(wait_sec, float(ra) + 0.35)
                except Exception:
                    pass
                # 1015 / edge limits: back off longer on last attempts
                if "1015" in err_body or "cloudflare" in err_body.lower():
                    wait_sec = max(wait_sec, 8.0 + attempt * 4.0)
                wait_sec = min(max(wait_sec, 1.0), 60.0)
                print(f"[relay] 429/rate-limit backoff sleeping {wait_sec:.2f}s")
                time.sleep(wait_sec)
                continue
            raise HTTPError(
                exc.url,
                exc.code,
                exc.msg,
                exc.headers,
                io.BytesIO(err_body.encode("utf-8")),
            ) from exc
    raise RuntimeError("Discord POST failed after retries")


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
        if self.path in ("/", "/health"):
            self._reply(
                200,
                {
                    "ok": True,
                    "service": "fenti-corpse-relay",
                    "bot_token_set": bool(BOT_TOKEN),
                    "channel_id_set": bool(CHANNEL_ID),
                    "relay_auth_required": bool(SHARED_SECRET),
                    "post_path": "/api/corpse-log",
                    "note": "Discord bots that only use HTTP to post messages stay offline in the member list; that is normal.",
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
                print(f"[relay] 401 bad Authorization header (len={len(auth)})")
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
            print(f"[relay] Discord HTTPError code={exc.code} detail={detail[:900]}")
            self._reply(
                502,
                {"ok": False, "error": "discord_http_error", "status": exc.code, "detail": detail},
            )
            return
        except URLError as exc:
            print(f"[relay] Discord URLError {exc}")
            self._reply(502, {"ok": False, "error": "discord_network_error", "detail": str(exc)})
            return
        except Exception as exc:
            print(f"[relay] relay_error {exc!r}")
            self._reply(500, {"ok": False, "error": "relay_error", "detail": str(exc)})
            return

        print("[relay] POST /api/corpse-log -> Discord ok")
        self._reply(200, {"ok": True})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    print(
        f"[relay] boot PORT={PORT} bot_token={'set' if BOT_TOKEN else 'MISSING'} "
        f"channel_id={'set' if CHANNEL_ID else 'MISSING'} shared_secret={'set' if SHARED_SECRET else 'off'}"
    )
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RelayHandler)
    print(f"[relay] listening 0.0.0.0:{PORT}")
    server.serve_forever()
