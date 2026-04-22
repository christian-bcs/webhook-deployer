from flask import Flask, request, abort, jsonify
import hmac
import hashlib
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

_webhook_secret = os.environ.get("WEBHOOK_SECRET")
if not _webhook_secret:
    raise RuntimeError("WEBHOOK_SECRET is not set. Add it to your environment or a .env file.")
WEBHOOK_SECRET = _webhook_secret.encode("utf-8")

_project_root = os.environ.get("PROJECT_ROOT")
if not _project_root:
    raise RuntimeError("PROJECT_ROOT is not set. Add it to your environment or a .env file.")
PROJECT_ROOT = Path(_project_root)

# only main branch triggers deploy
MAIN_REF = "refs/heads/main"


def resolve_repo_path(name: str | None) -> Path | None:
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    try:
        candidate = (PROJECT_ROOT / name).resolve()
        candidate.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return None
    return candidate


def verify_signature(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET,
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.route("/deploy", methods=["POST"])
def deploy():
    signature = request.headers.get("X-Hub-Signature-256")
    if not signature:
        abort(403, "Missing signature")

    payload = request.get_data()
    if not verify_signature(payload, signature):
        abort(403, "Invalid signature")

    try:
        data = json.loads(payload.decode("utf-8")) if payload else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "Invalid JSON body"}), 400

    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    repository = data.get("repository")
    if not isinstance(repository, dict):
        return jsonify({"error": "Missing repository in payload"}), 400

    name = repository.get("name")
    repo_path = resolve_repo_path(name)
    if repo_path is None:
        return jsonify({"error": "Invalid repository name"}), 400

    if not repo_path.is_dir():
        return jsonify(
            {
                "error": "Repository folder not found on server",
                "path": str(repo_path),
            }
        ), 404

    if data.get("ref") != MAIN_REF:
        return jsonify({"status": "ignored", "reason": "wrong branch"}), 200

    try:
        # run git pull
        result = subprocess.run(
            ["git", "-C", str(repo_path), "pull", "origin", "main"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            return (
                jsonify(
                    {
                        "status": "error",
                        "step": "git_pull",
                        "stderr": result.stderr,
                    }
                ),
                500,
            )

        return (
            jsonify(
                {
                    "status": "success",
                    "stdout": result.stdout,
                }
            ),
            200,
        )

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "step": "timeout"}), 500

    except Exception as e:
        return (
            jsonify(
                {
                    "status": "error",
                    "step": "exception",
                    "message": str(e),
                }
            ),
            500,
        )


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port)
