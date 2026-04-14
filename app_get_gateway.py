#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

app = Flask(__name__)

TOKEN = os.environ.get("TOKEN", "CHANGE_ME")
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", str(Path(__file__).resolve().parent / "storage")))
STATE_FILE = STORAGE_DIR / "state.json"
LOCK_FILE = STORAGE_DIR / ".gateway.lock"

ALLOWED_KEYS = {"Molecule", "Feedback"}
STOP_SENTINEL = "__CONTROL__:STOP"

STORAGE_DIR.mkdir(parents=True, exist_ok=True)
LOCK_FILE.touch(exist_ok=True)

_process_lock = threading.Lock()


def channel_path(key: str) -> Path:
    return STORAGE_DIR / f"{key}.yaml"


def no_store_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def default_channel_state() -> dict[str, Any]:
    return {
        "exists": False,
        "sealed": False,
        "size": 0,
        "sha256": None,
        "read_count": 0,
        "updated_at": None,
    }


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"Molecule": default_channel_state(), "Feedback": default_channel_state()}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    out = {"Molecule": default_channel_state(), "Feedback": default_channel_state()}
    for key in ALLOWED_KEYS:
        if isinstance(data.get(key), dict):
            out[key].update(data[key])
    return out


def save_state(state: dict[str, Any]) -> None:
    atomic_write_text(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


def file_info(path: Path) -> tuple[bool, int, str | None]:
    if not path.exists():
        return False, 0, None
    raw = path.read_bytes()
    return True, len(raw), hashlib.sha256(raw).hexdigest()


def sync_channel_state(state: dict[str, Any], key: str) -> None:
    exists, size, sha256 = file_info(channel_path(key))
    channel = state[key]
    channel["exists"] = exists
    channel["size"] = size
    channel["sha256"] = sha256
    if not exists:
        channel["sealed"] = False
        channel["read_count"] = 0
    channel["updated_at"] = int(time.time())


def sync_all_state(state: dict[str, Any]) -> None:
    for key in ALLOWED_KEYS:
        sync_channel_state(state, key)


@contextmanager
def locked_state():
    with _process_lock:
        with LOCK_FILE.open("r+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            state = load_state()
            sync_all_state(state)
            try:
                yield state
            finally:
                save_state(state)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def require_token() -> Response | None:
    token = request.args.get("token", "")
    if token != TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    return None


def require_key() -> tuple[str | None, Response | None]:
    key = request.args.get("key", "")
    if key not in ALLOWED_KEYS:
        return None, (jsonify({"ok": False, "error": "invalid key"}), 400)
    return key, None


def current_text(key: str) -> str:
    path = channel_path(key)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def is_stop_payload(text: str) -> bool:
    return text.strip() == STOP_SENTINEL


def response_json(payload: dict[str, Any], no_store: bool = False, status: int = 200):
    resp = jsonify(payload)
    resp.status_code = status
    if no_store:
        for k, v in no_store_headers().items():
            resp.headers[k] = v
    return resp


def compute_phase(state: dict[str, Any]) -> str:
    mol = state["Molecule"]
    fb = state["Feedback"]
    mol_text = current_text("Molecule") if mol["exists"] else ""
    if mol["sealed"] and is_stop_payload(mol_text):
        return "stop_ready"
    if not mol["exists"] and not fb["exists"]:
        return "idle"
    if mol["sealed"] and not fb["sealed"]:
        return "reviewing"
    if fb["sealed"] and not mol["sealed"]:
        return "awaiting_author"
    if mol["exists"] and not mol["sealed"] and not fb["sealed"]:
        return "authoring"
    return "mixed"


def can_push_molecule(state: dict[str, Any]) -> tuple[bool, str | None]:
    mol = state["Molecule"]
    fb = state["Feedback"]
    if mol["sealed"]:
        return False, "Molecule is sealed"
    if fb["sealed"]:
        return False, "Feedback is sealed; author must pull/reset Feedback before sending new Molecule"
    return True, None


def can_push_feedback(state: dict[str, Any]) -> tuple[bool, str | None]:
    mol = state["Molecule"]
    fb = state["Feedback"]
    if not mol["sealed"]:
        return False, "Molecule must be sealed before Feedback can be written"
    if fb["sealed"]:
        return False, "Feedback is already sealed"
    return True, None


def can_seal_molecule(state: dict[str, Any]) -> tuple[bool, str | None]:
    mol = state["Molecule"]
    fb = state["Feedback"]
    if fb["sealed"]:
        return False, "Feedback is still sealed"
    if mol["sealed"]:
        return False, "Molecule is already sealed"
    if not mol["exists"] or mol["size"] <= 0:
        return False, "Molecule is empty"
    return True, None


def can_seal_feedback(state: dict[str, Any]) -> tuple[bool, str | None]:
    mol = state["Molecule"]
    fb = state["Feedback"]
    if not mol["sealed"]:
        return False, "Molecule must be sealed before Feedback can be sealed"
    if fb["sealed"]:
        return False, "Feedback is already sealed"
    if not fb["exists"] or fb["size"] <= 0:
        return False, "Feedback is empty"
    return True, None


def can_reset_molecule(state: dict[str, Any], force: bool) -> tuple[bool, str | None]:
    if force:
        return True, None
    if not state["Molecule"]["exists"]:
        return True, None
    if state["Feedback"]["sealed"]:
        return True, None
    return False, "Molecule can be reset only after Feedback is sealed, or with force=1"


def can_reset_feedback(state: dict[str, Any], force: bool) -> tuple[bool, str | None]:
    if force:
        return True, None

    if not state["Feedback"]["exists"]:
        return True, None

    fb = state["Feedback"]
    mol = state["Molecule"]

    # Feedback reset should not depend on pull/read side effects.
    # If Feedback is sealed, explicit reset is allowed.
    if fb["sealed"]:
        return True, None

    if mol["sealed"] and is_stop_payload(current_text("Molecule")):
        return True, None

    return False, "Feedback can be reset only after it is sealed, or with force=1"


@app.get("/")
def home():
    html = f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Znagoria GET Gateway v2</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; }}
            textarea {{ width: 100%; height: 180px; }}
            input, select, button {{ margin: 4px 0; padding: 8px; }}
            .row {{ margin-bottom: 12px; }}
            .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; }}
        </style>
    </head>
    <body>
        <h1>Znagoria GET Gateway v2</h1>
        <div class="row"><label>Token: <input id="token" placeholder="TOKEN"></label></div>
        <div class="row"><label>Key:
            <select id="key"><option>Molecule</option><option>Feedback</option></select>
        </label></div>
        <div class="row"><label>Body:</label><textarea id="body" placeholder="Write chunk text here"></textarea></div>
        <div class="row">
            <button onclick="go('push')">Push</button>
            <button onclick="go('seal')">Seal</button>
            <button onclick="go('pull')">Pull</button>
            <button onclick="go('status')">Status</button>
            <button onclick="go('reset')">Reset</button>
            <button onclick="go('reset', true)">Force Reset</button>
        </div>
        <h3>Result</h3>
        <div id="out" class="mono"></div>
        <script>
            async function go(action, force=false) {{
                const token = document.getElementById('token').value;
                const key = document.getElementById('key').value;
                const bodyEl = document.getElementById('body');
                const outEl = document.getElementById('out');
                const body = bodyEl.value;

                const params = new URLSearchParams({{ token }});
                if (action !== 'status') params.set('key', key);
                if (action === 'push') params.set('body', body);
                if (force) params.set('force', '1');

                const resp = await fetch('/' + action + '?' + params.toString(), {{ cache: 'no-store' }});
                const text = await resp.text();

                if (action === 'pull' || action === 'status') {{
                    outEl.textContent = text;
                    return;
                }}

                if (action === 'reset' && resp.ok) {{
                    bodyEl.value = '';
                }}

                const statusResp = await fetch('/status?token=' + encodeURIComponent(token), {{ cache: 'no-store' }});
                const statusText = await statusResp.text();

                outEl.textContent = text + "\n\nPOST_ACTION_STATUS:\n" + statusText;
            }}
        </script>
    </body>
    </html>
    """
    resp = Response(html, mimetype="text/html")
    for k, v in no_store_headers().items():
        resp.headers[k] = v
    return resp


@app.get("/status")
def status():
    auth = require_token()
    if auth:
        return auth
    with locked_state() as state:
        return response_json({
            "ok": True,
            "phase": compute_phase(state),
            "Molecule": state["Molecule"],
            "Feedback": state["Feedback"],
            "host": os.uname().nodename,
            "pid": os.getpid(),
            "storage_dir": str(STORAGE_DIR),
        }, no_store=True)


@app.get("/push")
def push():
    auth = require_token()
    if auth:
        return auth
    key, err = require_key()
    if err:
        return err
    body = request.args.get("body", "")
    with locked_state() as state:
        ok, why = can_push_molecule(state) if key == "Molecule" else can_push_feedback(state)
        if not ok:
            return response_json({"ok": False, "action": "push", "key": key, "error": why}, status=409, no_store=True)
        with channel_path(key).open("a", encoding="utf-8") as fh:
            fh.write(body)
        sync_channel_state(state, key)
        state[key]["updated_at"] = int(time.time())
        return response_json({
            "ok": True,
            "action": "push",
            "key": key,
            "sealed": state[key]["sealed"],
            "size": state[key]["size"],
        }, no_store=True)


@app.get("/seal")
def seal():
    auth = require_token()
    if auth:
        return auth
    key, err = require_key()
    if err:
        return err
    with locked_state() as state:
        # Idempotent seal: repeated GET should not break the flow.
        if state[key]["sealed"]:
            sync_channel_state(state, key)
            return response_json({
                "ok": True,
                "action": "seal",
                "key": key,
                "sealed": True,
                "already_sealed": True,
                "size": state[key]["size"],
                "sha256": state[key]["sha256"],
            }, no_store=True)

        ok, why = can_seal_molecule(state) if key == "Molecule" else can_seal_feedback(state)
        if not ok:
            return response_json({"ok": False, "action": "seal", "key": key, "error": why}, status=409, no_store=True)

        state[key]["sealed"] = True
        sync_channel_state(state, key)
        return response_json({
            "ok": True,
            "action": "seal",
            "key": key,
            "sealed": True,
            "size": state[key]["size"],
            "sha256": state[key]["sha256"],
        }, no_store=True)


@app.get("/pull")
def pull():
    auth = require_token()
    if auth:
        return auth
    key, err = require_key()
    if err:
        return err
    with locked_state() as state:
        if not state[key]["sealed"]:
            return response_json({"ok": False, "action": "pull", "key": key, "error": f"{key} is not sealed"}, status=409, no_store=True)
        # pull must stay read-only; no read_count increment here
        text = current_text(key)
        resp = Response(text, content_type="text/plain; charset=utf-8")
        for k, v in no_store_headers().items():
            resp.headers[k] = v
        return resp


@app.get("/reset")
def reset():
    auth = require_token()
    if auth:
        return auth
    key, err = require_key()
    if err:
        return err
    force = request.args.get("force", "0") == "1"
    with locked_state() as state:
        ok, why = can_reset_molecule(state, force) if key == "Molecule" else can_reset_feedback(state, force)
        if not ok:
            return response_json({"ok": False, "action": "reset", "key": key, "error": why}, status=409, no_store=True)
        path = channel_path(key)
        if path.exists():
            path.unlink()
        state[key] = default_channel_state()
        state[key]["updated_at"] = int(time.time())
        return response_json({"ok": True, "action": "reset", "key": key, "forced": force}, no_store=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
