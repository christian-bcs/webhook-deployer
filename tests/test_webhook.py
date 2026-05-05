import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import main
from .helpers import sign_payload


def _payload(
    *,
    ref: str | None = "refs/heads/main",
    repo_name: str = "my-app",
    omit_ref: bool = False,
) -> bytes:
    body: dict[str, object] = {"repository": {"name": repo_name}}
    if not omit_ref:
        body["ref"] = ref
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


class TestPullBranchFromRef:
    def test_missing_defaults_main(self):
        b, ign, err = main.pull_branch_from_ref(None)
        assert (b, ign, err) == ("main", None, None)

    def test_empty_string_defaults_main(self):
        b, ign, err = main.pull_branch_from_ref("   ")
        assert (b, ign, err) == ("main", None, None)

    def test_refs_heads_full(self):
        b, ign, err = main.pull_branch_from_ref("refs/heads/staging")
        assert (b, ign, err) == ("staging", None, None)

    def test_shorthand_branch(self):
        b, ign, err = main.pull_branch_from_ref("main")
        assert (b, ign, err) == ("main", None, None)

    def test_non_heads_refs_ignored(self):
        b, ign, err = main.pull_branch_from_ref("refs/tags/v1.0")
        assert (b, ign, err) == (None, "tag ref", None)

        b2, ign2, err2 = main.pull_branch_from_ref("refs/remotes/origin/main")
        assert (b2, ign2, err2) == (None, "unsupported ref", None)

    def test_invalid_ref_type(self):
        b, ign, err = main.pull_branch_from_ref(123)
        assert (b, ign, err) == (None, None, "Invalid ref")

    @pytest.mark.parametrize(
        "ref",
        ["refs/heads/", "refs/heads/../x", "refs/heads/\\x", "..\\main", "\\foo"],
    )
    def test_invalid_branch_segment(self, ref):
        b, ign, err = main.pull_branch_from_ref(ref)
        assert err == "Invalid ref"
        assert b is None and ign is None


class TestDeployFlow:
    def test_400_invalid_ref_over_http(self, client, project_root):
        d = project_root / "my-app"
        d.mkdir(exist_ok=True)
        body = _payload(ref="refs/heads/../x")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 400
        assert r.get_json()["error"] == "Invalid ref"

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

    def test_200_ignored_tag_ref(self, client, project_root):
        d = project_root / "ig-app"
        d.mkdir(exist_ok=True)
        body = _payload(ref="refs/tags/v1", repo_name="ig-app")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200
        j = r.get_json()
        assert j["status"] == "ignored"
        assert j["reason"] == "tag ref"

    @patch("main.subprocess.run")
    def test_200_pull_branch_not_only_main(self, mock_run, client, project_root):
        d = project_root / "ig-app"
        d.mkdir(exist_ok=True)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        body = _payload(ref="refs/heads/staging", repo_name="ig-app")
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
        assert j["branch"] == "staging"
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][-3:] == ["pull", "origin", "staging"]

    @patch("main.subprocess.run")
    def test_200_success(self, mock_run, client, project_root):
        d = project_root / "ok-app"
        d.mkdir(exist_ok=True)
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
        assert j["branch"] == "main"
        assert "Already up to date" in j["stdout"]
        mock_run.assert_called_once()
        call = mock_run.call_args[0][0]
        assert call[0] == "git" and call[1] == "-C"
        assert Path(call[2]).resolve() == d.resolve()
        assert call[-3:] == ["pull", "origin", "main"]

    @patch("main.subprocess.run")
    def test_200_optional_ref_defaults_main(self, mock_run, client, project_root):
        d = project_root / "ok-app"
        d.mkdir(exist_ok=True)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        body = _payload(repo_name="ok-app", omit_ref=True)
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200
        assert r.get_json()["branch"] == "main"
        assert mock_run.call_args[0][0][-1] == "main"

    @patch("main.subprocess.run")
    def test_200_shorthand_ref_main(self, mock_run, client, project_root):
        d = project_root / "ok-app"
        d.mkdir(exist_ok=True)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        body = _payload(ref="main", repo_name="ok-app")
        sig = sign_payload("test-hmac-key", body)
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert r.status_code == 200
        assert r.get_json()["branch"] == "main"

    @patch("main.subprocess.run")
    def test_500_git_fails(self, mock_run, client, project_root):
        d = project_root / "bad-app"
        d.mkdir(exist_ok=True)
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


class TestAuditLog:
    @staticmethod
    def _new_audit_lines(log_path: Path) -> list[str]:
        if not log_path.is_file():
            return []
        return log_path.read_text(encoding="utf-8").splitlines()

    @patch("main.subprocess.run")
    def test_git_pull_ok_writes_audit_line(self, mock_run, client, project_root, audit_log_dir):
        log_path = audit_log_dir / "deployer.log"
        before = len(self._new_audit_lines(log_path))

        d = project_root / "ok-app"
        d.mkdir(exist_ok=True)
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
        lines = self._new_audit_lines(log_path)
        delta = lines[before:]
        assert delta
        row = json.loads(delta[-1])
        assert row["event"] == "git_pull_ok"
        assert row["repo"] == "ok-app"
        assert row["branch"] == "main"
        assert "ts" in row

    def test_invalid_signature_writes_audit_line(self, client, audit_log_dir):
        log_path = audit_log_dir / "deployer.log"
        before = len(self._new_audit_lines(log_path))

        body = _payload()
        r = client.post(
            "/deploy",
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": "sha256=bad"},
        )
        assert r.status_code == 403
        lines = self._new_audit_lines(log_path)
        delta = lines[before:]
        assert delta
        row = json.loads(delta[-1])
        assert row["event"] == "request_denied"
        assert row["reason"] == "invalid_signature"

