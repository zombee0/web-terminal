"""Configuration loading without evaluating shell code or exposing credentials."""

import json
import os
import pathlib
import re
import shlex
import tempfile
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE = ROOT / ".web-terminal" / "supabase"
ENV_NAMES = {
    "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SECRET_KEY",
    "SUPABASE_DB_URL", "SUPABASE_ACCESS_TOKEN", "BOXLITE_API_KEY", "BOXLITE_REST_URL",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
}


def load_deployment_env():
    """Read literal exports; never source a user's interactive shell configuration."""
    path = pathlib.Path.home() / ".zshrc"
    if path.is_file():
        for line in path.read_text().splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$", line)
            if match and match[1] in ENV_NAMES:
                parts = shlex.split(match[2], comments=True)
                if len(parts) == 1:
                    os.environ[match[1]] = parts[0]
    os.environ.setdefault("BOXLITE_REST_URL", "https://app.boxlite.ai/api")
    os.environ["BOXLITE_RUNTIME"] = "cloud"
    os.environ["RUST_LOG"] = "off"


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required variable: {name}")
    return value


def project_ref():
    url = urlsplit(required("SUPABASE_URL"))
    if url.scheme != "https" or not re.fullmatch(r"[a-z0-9]+\.supabase\.co", url.hostname or ""):
        raise ValueError("Invalid SUPABASE_URL")
    return url.hostname.split(".")[0]


def save_private(path, data):
    """Atomically persist deployment recovery state outside version control."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_private(path, default=None):
    path = pathlib.Path(path)
    return json.loads(path.read_text()) if path.exists() else default
