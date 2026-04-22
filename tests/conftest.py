from __future__ import annotations

import os
import tempfile
from pathlib import Path

# env must be set before main is imported (load_dotenv does not override these)
os.environ["WEBHOOK_SECRET"] = "test-hmac-key"
os.environ["PROJECT_ROOT"] = tempfile.mkdtemp()
os.environ["ALLOWED_REPOS"] = "my-app,nonexistent,ig-app,ok-app,bad-app"

import main as main

import pytest


@pytest.fixture
def app():
    main.app.config["TESTING"] = True
    yield main.app
    main.app.config["TESTING"] = False


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def project_root() -> Path:
    return main.PROJECT_ROOT
