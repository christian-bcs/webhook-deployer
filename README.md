# Webhook deployer

Small Flask service that accepts **GitHub webhooks** and runs **`git pull origin main`** in a directory under `PROJECT_ROOT`. The directory name is **`repository.name`** from the webhook JSON (e.g. `repo-one` → `PROJECT_ROOT/repo-one`). Only pushes to **`main`** (ref `refs/heads/main`) are handled.

## Setup

1. **Python 3.10+** (uses `Path | None` type hints).
2. Create a virtual environment and install dependencies:

   ```text
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Copy **`.env.example`** to **`.env`** and set at least:
   - **`WEBHOOK_SECRET`** – same value as the secret you configure in the GitHub webhook.
   - **`PROJECT_ROOT`** – absolute path to the parent directory that contains one subdirectory per repository clone; each subdirectory’s name must match the GitHub repository name.
   - **`ALLOWED_REPOS`** – comma-separated list of **`repository.name`** values that are allowed to deploy. The list must be non-empty. Pushes from any other repository are rejected with **403**, even when the webhook signature is valid (e.g. same org secret used on many repos). Names are matched after trimming whitespace and are **case-sensitive** (use the exact name GitHub reports).

4. For each project, on the server, clone (or have) the repo at **`PROJECT_ROOT / <github-repo-name>`** (e.g. `.../projects/repo-one`). The user running this app must be able to run `git pull` there.

5. Run:

   ```text
   python main.py
   ```

   Optional: **`HOST`** and **`PORT`** in `.env` (defaults `0.0.0.0` and `5000`).

## GitHub webhook

- **URL:** `https://<your-host>/deploy` (same URL can be used for every repository webhook).
- **Content type:** `application/json`.
- **Secret:** set to the same string as `WEBHOOK_SECRET`.
- Pushes to branches other than **main** are ignored (HTTP 200 with `status: "ignored"`).
- The payload’s **`repository.name`** must appear in **`ALLOWED_REPOS`**, or the service responds with **403**.
- The payload’s **`repository.name`** must also match an existing directory under `PROJECT_ROOT`, or the service responds with **404** and does not run `git pull`.

## Testing

Install dev dependencies (includes `pytest`):

```text
pip install -r requirements-dev.txt
```

Run the suite from the project root:

```text
python -m pytest
```

`tests/conftest.py` sets `WEBHOOK_SECRET`, a temporary `PROJECT_ROOT`, and `ALLOWED_REPOS` so your `.env` is not required. `subprocess.run` (git) is mocked for the success and failure cases.

## Production

Run behind a reverse proxy (HTTPS, rate limiting) and as a **systemd** (or similar) service, not in the Flask dev server, if you expose this to the internet. Restrict who can reach the deploy URL.
