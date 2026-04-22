import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import main
from .helpers import sign_payload


def _payload(
    ref: str = "refs/heads/main",
    repo_name: str = "my-app",
) -> bytes:
    body = {
        "ref": ref,
        "repository": {"name": repo_name},
    }
    return json.dumps(body).encode("utf-8")


class TestAllowlist:
    def test_403_not_allowlisted(self, client):
        body = _payload(repo_name="not-in-allowlist")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 403
        assert r.get_json()["error"] == "Repository not allowlisted"


class TestAuth:
    def test_403_missing_signature(self, client):
        r = client.post("/deploy", data=_payload())
        assert r.status_code == 403

    def test_403_invalid_signature(self, client):
        body = _payload()
        r = client.post(
            "/deploy",
            data=body,
            headers={"X-Hub-Signature-256": "sha256=bad"},
        )
        assert r.status_code == 403


class TestBody:
    def test_400_invalid_json(self, client):
        body = b"not json"
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 400
        assert b"Invalid JSON" in r.data

    def test_400_missing_repository(self, client):
        body = json.dumps({"ref": "refs/heads/main"}).encode("utf-8")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 400


class TestResolveRepo:
    @pytest.mark.parametrize(
        "name",
        [None, "", "../x", "a/b", "a\\b"],
    )
    def test_invalid_name(self, name):
        assert main.resolve_repo_path(name) is None

    def test_valid_name(self, project_root, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
        p = main.resolve_repo_path("valid-repo")
        assert p is not None
        assert p == (tmp_path / "valid-repo").resolve()


class TestDeployFlow:
    def test_404_no_such_directory(self, client, project_root):
        body = _payload(repo_name="nonexistent")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 404
        data = r.get_json()
        assert data == {"error": "Repository folder not found on server"}
        assert "path" not in data

    def test_200_ignored_branch(self, client, project_root):
        d = project_root / "ig-app"
        d.mkdir()
        body = _payload(ref="refs/heads/staging", repo_name="ig-app")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200
        assert r.get_json()["status"] == "ignored"

    @patch("main.subprocess.run")
    def test_200_success(self, mock_run, client, project_root):
        d = project_root / "ok-app"
        d.mkdir()
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="Already up to date.\n",
            stderr="",
        )
        body = _payload(repo_name="ok-app")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["status"] == "success"
        assert "Already up to date" in j["stdout"]
        mock_run.assert_called_once()
        call = mock_run.call_args[0][0]
        assert call[0] == "git" and call[1] == "-C"
        assert Path(call[2]).resolve() == d.resolve()

    @patch("main.subprocess.run")
    def test_500_git_fails(self, mock_run, client, project_root):
        d = project_root / "bad-app"
        d.mkdir()
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"],
            returncode=1,
            stdout="",
            stderr="merge conflict",
        )
        body = _payload(repo_name="bad-app")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 500
        j = r.get_json()
        assert j == {"status": "error", "step": "git_pull"}
        assert "merge conflict" not in r.data.decode("utf-8").lower()
