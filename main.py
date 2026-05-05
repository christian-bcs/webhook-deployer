from flask import Flask, request, abort, jsonify
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)
audit_log = logging.getLogger("deployer.audit")

app = Flask(__name__)

_webhook_secret = os.environ.get("WEBHOOK_SECRET")
if not _webhook_secret:
    raise RuntimeError("WEBHOOK_SECRET is not set. Add it to your environment or a .env file.")
WEBHOOK_SECRET = _webhook_secret.encode("utf-8")

_project_root = os.environ.get("PROJECT_ROOT")
if not _project_root:
    raise RuntimeError("PROJECT_ROOT is not set. Add it to your environment or a .env file.")
PROJECT_ROOT = Path(_project_root)

_raw_allowed = os.environ.get("ALLOWED_REPOS", "")
ALLOWED_REPOS: frozenset[str] = frozenset(
    p.strip() for p in _raw_allowed.split(",") if p.strip()
)
if not ALLOWED_REPOS:
    raise RuntimeError(
        "ALLOWED_REPOS is not set or empty. Set a comma-separated list of "
        "GitHub repository names (repository.name) that may deploy here."
    )

AUDIT_LOG_DIR = Path(__file__).resolve().parent / "logs"


def configure_audit_logging(log_dir: Path | None = None) -> None:
    """daily rotated JSON audit log under project logs/ (override log_dir for tests only)."""
    path = AUDIT_LOG_DIR if log_dir is None else log_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("audit log dir unusable: %s (%s)", path, exc)
        return
    try:
        backup_days = max(1, min(3650, int(os.environ.get("LOG_BACKUP_DAYS", "30"))))
    except ValueError:
        backup_days = 90
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(path / "deployer.log"),
        when="midnight",
        interval=1,
        backupCount=backup_days,
        utc=True,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_log.handlers.clear()
    audit_log.addHandler(handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False


def audit_record(event: str, **fields: object) -> None:
    line = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        **{k: v for k, v in fields.items() if v is not None},
    }
    audit_log.info(json.dumps(line, separators=(",", ":"), ensure_ascii=False))


configure_audit_logging()

DEFAULT_BRANCH = "main"


def _branch_name_invalid(name: str) -> bool:
    if not name or "\\" in name or ".." in name or "\x00" in name:
        return True
    if name.startswith("/"):
        return True
    return False


def pull_branch_from_ref(ref: object) -> tuple[str | None, str | None, str | None]:
    """Map webhook ref to branch for git pull.

    Returns (branch, ignored_reason, bad_request_error). Exactly one outcome:
    branch set; or ignored_reason for HTTP 200 ignored; or bad_request_error for 400.
    """
    if ref is None:
        return (DEFAULT_BRANCH, None, None)
    if not isinstance(ref, str):
        return (None, None, "Invalid ref")
    r = ref.strip()
    if not r:
        return (DEFAULT_BRANCH, None, None)
    if r.startswith("refs/heads/"):
        b = r[len("refs/heads/") :].strip()
        if _branch_name_invalid(b):
            return (None, None, "Invalid ref")
        return (b, None, None)
    if r.startswith("refs/tags/"):
        return (None, "tag ref", None)
    if r.startswith("refs/"):
        return (None, "unsupported ref", None)
    if _branch_name_invalid(r):
        return (None, None, "Invalid ref")
    return (r, None, None)


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
        audit_record(
            "request_denied",
            reason="missing_signature",
            remote_addr=request.remote_addr,
        )
        abort(403, "Missing signature")

    payload = request.get_data()
    if not verify_signature(payload, signature):
        audit_record(
            "request_denied",
            reason="invalid_signature",
            remote_addr=request.remote_addr,
        )
        abort(403, "Invalid signature")

    try:
        data = json.loads(payload.decode("utf-8")) if payload else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        audit_record("payload_invalid", reason="bad_json")
        return jsonify({"error": "Invalid JSON body"}), 400

    if not isinstance(data, dict):
        audit_record("payload_invalid", reason="not_object")
        return jsonify({"error": "Invalid JSON body"}), 400

    repository = data.get("repository")
    if not isinstance(repository, dict):
        audit_record("payload_invalid", reason="missing_repository")
        return jsonify({"error": "Missing repository in payload"}), 400

    name = repository.get("name")
    if not isinstance(name, str):
        audit_record("repository_invalid", detail="name_not_string")
        return jsonify({"error": "Invalid repository name"}), 400
    n = name.strip()
    if not n:
        audit_record("repository_invalid", detail="name_empty")
        return jsonify({"error": "Invalid repository name"}), 400
    if n not in ALLOWED_REPOS:
        audit_record("repository_forbidden", repo=n)
        return jsonify({"error": "Repository not allowlisted"}), 403

    repo_path = resolve_repo_path(n)
    if repo_path is None:
        audit_record("repository_invalid", repo=n, detail="unsafe_or_bad_path")
        return jsonify({"error": "Invalid repository name"}), 400

    if not repo_path.is_dir():
        log.warning("repository folder not found: %s", repo_path)
        audit_record("repository_missing", repo=n)
        return jsonify({"error": "Repository folder not found on server"}), 404

    branch, ignored_reason, ref_error = pull_branch_from_ref(data.get("ref"))
    ref_raw = data.get("ref")
    if isinstance(ref_raw, str):
        ref_in = ref_raw.strip() or None
    else:
        ref_in = ref_raw if ref_raw is None else str(ref_raw)
    if ref_error:
        audit_record("ref_invalid", repo=n, ref=ref_in, detail=ref_error)
        return jsonify({"error": ref_error}), 400
    if ignored_reason:
        audit_record(
            "deploy_skipped",
            repo=n,
            reason=ignored_reason,
            ref=ref_in,
        )
        return jsonify({"status": "ignored", "reason": ignored_reason}), 200

    assert branch is not None

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "pull", "origin", branch],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            log.error("git pull failed for %s: %s", repo_path, result.stderr)
            audit_record(
                "git_pull_failed",
                repo=n,
                branch=branch,
                stderr_tail=(result.stderr or "")[-500:] or None,
            )
            return jsonify({"status": "error", "step": "git_pull"}), 500

        audit_record("git_pull_ok", repo=n, branch=branch)
        return (
            jsonify(
                {
                    "status": "success",
                    "branch": branch,
                    "stdout": result.stdout,
                }
            ),
            200,
        )

    except subprocess.TimeoutExpired:
        log.error("git pull timed out for %s", repo_path)
        audit_record("git_pull_failed", repo=n, branch=branch, detail="timeout")
        return jsonify({"status": "error", "step": "timeout"}), 500

    except Exception:
        log.exception("deploy failed for %s", repo_path)
        audit_record("git_pull_failed", repo=n, branch=branch, detail="exception")
        return jsonify({"status": "error", "step": "exception"}), 500


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port)
