from __future__ import annotations

import json
import re
import os
from pathlib import Path
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
STATE_FILE = STORAGE_DIR / "state.json"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_KEYS = {"Molecule", "Feedback"}
TOKEN = "CHANGE_ME"


def normalize_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().strip('"').strip("'")
    return value if value in ALLOWED_KEYS else None


def yaml_path(key: str) -> Path:
    return STORAGE_DIR / f"{key}.yaml"


def default_state() -> dict:
    return {
        key: {
            "sealed": False,
            "size": yaml_path(key).stat().st_size if yaml_path(key).exists() else 0,
        }
        for key in sorted(ALLOWED_KEYS)
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        state = default_state()
        save_state(state)
        return state
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = default_state()
        save_state(state)
        return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def require_token() -> Response | None:
    token = request.args.get("token", "")
    if token != TOKEN:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


@app.get("/")
def home():
    return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>GET Gateway</title>
  <style>
    body { font-family: sans-serif; max-width: 900px; margin: 40px auto; padding: 0 16px; }
    textarea { width: 100%; min-height: 180px; }
    input, select { padding: 8px; }
    button { padding: 8px 12px; margin-right: 8px; }
    pre { background: #f5f5f5; padding: 12px; overflow: auto; white-space: pre-wrap; }
    .row { margin: 12px 0; }
  </style>
</head>
<body>
  <h1>GET Gateway</h1>
  <div class="row">
    <label>Token: <input id="token" value="CHANGE_ME"></label>
    <label>Key:
      <select id="key">
        <option>Molecule</option>
        <option>Feedback</option>
      </select>
    </label>
  </div>
  <div class="row">
    <textarea id="body" placeholder="Текст для дописывания"></textarea>
  </div>
  <div class="row">
    <button onclick="pushChunk()">Push</button>
    <button onclick="sealKey()">Seal</button>
    <button onclick="pullKey()">Pull</button>
    <button onclick="showStatus()">Status</button>
  </div>
  <pre id="out"></pre>
<script>
const out = document.getElementById('out');
function q(id){ return document.getElementById(id).value; }
async function hit(path){
  const r = await fetch(path);
  const text = await r.text();
  out.textContent = text;
}
function enc(v){ return encodeURIComponent(v); }
function pushChunk(){
  hit(`/push?token=${enc(q('token'))}&key=${enc(q('key'))}&body=${enc(q('body'))}`);
}
function sealKey(){
  hit(`/seal?token=${enc(q('token'))}&key=${enc(q('key'))}`);
}
function pullKey(){
  hit(`/pull?token=${enc(q('token'))}&key=${enc(q('key'))}`);
}
function showStatus(){
  hit(`/status?token=${enc(q('token'))}`);
}
</script>
</body>
</html>
"""


@app.get("/push")
def push():
    forbidden = require_token()
    if forbidden:
        return forbidden

    key = normalize_key(request.args.get("key"))
    if not key:
        return jsonify({"ok": False, "error": "invalid key"}), 400

    body = request.args.get("body")
    if body is None:
        return jsonify({"ok": False, "error": "body is required"}), 400

    state = load_state()
    path = yaml_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as f:
        f.write(body)

    state[key]["size"] = path.stat().st_size
    save_state(state)
    return jsonify({"ok": True, "action": "push", "key": key, "sealed": state[key]["sealed"], "size": state[key]["size"]})


@app.get("/seal")
def seal():
    forbidden = require_token()
    if forbidden:
        return forbidden

    key = normalize_key(request.args.get("key"))
    if not key:
        return jsonify({"ok": False, "error": "invalid key"}), 400

    state = load_state()
    state[key]["sealed"] = True
    path = yaml_path(key)
    state[key]["size"] = path.stat().st_size if path.exists() else 0
    save_state(state)
    return jsonify({"ok": True, "action": "seal", "key": key, "sealed": True, "size": state[key]["size"]})


@app.get("/pull")
def pull():
    forbidden = require_token()
    if forbidden:
        return forbidden

    key = normalize_key(request.args.get("key"))
    if not key:
        return jsonify({"ok": False, "error": "invalid key"}), 400

    state = load_state()
    if not state.get(key, {}).get("sealed"):
        return jsonify({"ok": False, "error": "not sealed yet", "key": key}), 409

    path = yaml_path(key)
    if not path.exists():
        return Response("", mimetype="text/plain; charset=utf-8")

    return Response(path.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8")


@app.get("/status")
def status():
    forbidden = require_token()
    if forbidden:
        return forbidden

    state = load_state()
    for key in ALLOWED_KEYS:
        path = yaml_path(key)
        state[key]["exists"] = path.exists()
        state[key]["size"] = path.stat().st_size if path.exists() else 0
    return jsonify({"ok": True, "state": state})


@app.get("/reset")
def reset():
    forbidden = require_token()
    if forbidden:
        return forbidden

    key = normalize_key(request.args.get("key"))
    if not key:
        return jsonify({"ok": False, "error": "invalid key"}), 400

    path = yaml_path(key)
    if path.exists():
        path.unlink()

    state = load_state()
    state[key] = {"sealed": False, "size": 0}
    save_state(state)
    return jsonify({"ok": True, "action": "reset", "key": key})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
