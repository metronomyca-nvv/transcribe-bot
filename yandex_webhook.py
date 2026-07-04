import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

HOST = os.getenv("YANDEX_WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.getenv("YANDEX_WEBHOOK_PORT", "8080"))
PATH = os.getenv("YANDEX_WEBHOOK_PATH", "/webhooks/yandex")
CAPTURE_PATH = DATA_DIR / "yandex_webhook_last.json"

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "x-api-key",
    "x-auth-token",
}


def _redact_headers(headers) -> dict:
    safe_headers: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            safe_headers[key] = "<redacted>"
        else:
            safe_headers[key] = value
    return safe_headers


async def healthcheck(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "yandex-webhook"})


async def receive_webhook(request: web.Request) -> web.Response:
    raw_body = await request.text()
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw_body}

    event = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "path": request.path,
        "query": dict(request.query),
        "headers": _redact_headers(request.headers),
        "payload": payload,
    }
    CAPTURE_PATH.write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")

    top_level_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    log.info(
        "Captured Yandex webhook request path=%s keys=%s",
        request.path,
        ",".join(top_level_keys),
    )
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", healthcheck)
    app.router.add_post(PATH, receive_webhook)
    return app


if __name__ == "__main__":
    log.info("Starting Yandex webhook receiver on %s:%s%s", HOST, PORT, PATH)
    web.run_app(build_app(), host=HOST, port=PORT)
