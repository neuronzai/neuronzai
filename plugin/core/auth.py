#!/usr/bin/env python3
"""Cross-host OAuth credentials for Neuronz.ai plugin processes.

The host-facing MCP proxy and lifecycle hooks share this module.  It performs
OAuth authorization-code + PKCE login, refreshes short-lived access tokens, and
stores the resulting grant outside the versioned plugin package.  Environment
bearers remain a compatibility fallback, but are never persisted by this code.

Pure stdlib so the marketplace package has no install-time dependency step.
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import hashlib
import http.server
import json
import os
import platform
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


# The managed server this plugin talks to when NEURONZAI_URL is unset. This line is
# the STAGING value: the in-repo (Gitea) marketplace installs the plugin straight
# from this source. The production marketplace is a separate, generated tree, and
# `marketplace_tree.py` restamps exactly this one line with the production URL while
# building it — keep it a single `DEFAULT_BASE_URL = "<url>"` line at column 0.
DEFAULT_BASE_URL = "https://app.neuronz.ai"
OAUTH_SCOPE = "openid profile email offline_access"
KEYRING_SERVICE = "ai.neuronz.oauth"
STORE_VERSION = 1
REFRESH_SKEW_SECONDS = 90
LOCK_WAIT_SECONDS = 1.0
REFRESH_TIMEOUT_SECONDS = 5
ENVIRONMENT_CREDENTIAL_NAMES = (
    "NEURONZAI_API_KEY",
    "NEURONZAI_TOKEN",
    "AGENT_TOKEN",
)
# Out-of-band login (#237): on a machine with no browser there is no loopback the
# user's browser could reach, so we register this page on the server as the
# redirect target and the user copies the code back into the terminal.
OOB_REDIRECT_PATH = "/oauth/complete"
# Escape hatch for a machine where detection still guesses wrong (a display is
# advertised but unreachable, an X forward that cannot render). Per-machine
# capability, so it is genuinely local — not server-resolved runtime config.
FORCE_NO_BROWSER_ENV = "NEURONZAI_NO_BROWSER"
# The window between "print the URL" and "paste the code back". Long enough for a
# real sign-in on another device, short enough that an abandoned attempt does not
# leave a usable verifier lying around.
PENDING_TTL_SECONDS = 600
# The completion page renders "<state>.<code>". state is hex (see the mint site),
# which never contains a dot, so splitting on the FIRST dot recovers both halves.
PAIRING_SEPARATOR = "."


class AuthError(RuntimeError):
    """A safe, user-facing authentication error (never contains credentials)."""


class CredentialBusy(RuntimeError):
    """Another plugin process owns the credential lock past our fail-open bound."""


def base_url() -> str:
    return (os.environ.get("NEURONZAI_URL") or DEFAULT_BASE_URL).rstrip("/")


def environment_token_names() -> list[str]:
    """Active compatibility credential names, never their secret values."""
    return [name for name in ENVIRONMENT_CREDENTIAL_NAMES if os.environ.get(name)]


def environment_token() -> str:
    for name in ENVIRONMENT_CREDENTIAL_NAMES:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _format_environment_names(names: list[str]) -> str:
    quoted = [f"`{name}`" for name in names]
    if len(quoted) < 2:
        return quoted[0] if quoted else "an environment credential"
    return ", ".join(quoted[:-1]) + f" and {quoted[-1]}"


def environment_override_message(
    action: str,
    names: list[str] | None = None,
    *,
    removed: bool = False,
) -> str:
    """Safe user-facing override guidance; variable names only, never values."""
    active = environment_token_names() if names is None else list(names)
    if not active:
        raise ValueError("environment override guidance requires an active credential variable")
    rendered = _format_environment_names(active)
    if len(active) == 1:
        login_subject = f"the credential variable {rendered} set"
        status_subject = f"the credential variable {rendered}"
        pronoun = "it"
        precedence = "The environment credential takes precedence over saved OAuth."
    else:
        login_subject = f"these credential variables set: {rendered}"
        status_subject = f"these credential variables: {rendered}"
        pronoun = "them"
        precedence = (
            "Environment credentials take precedence over saved OAuth in the order shown."
        )
    restart = (
        f"Remove {pronoun} from the environment that launches the agent, then start "
        "a new agent session"
    )
    if action == "login":
        return (
            f"OAuth is saved, but this coding-agent process still has {login_subject}. "
            f"{precedence} {restart} to use OAuth."
        )
    if action == "status":
        return (
            "Neuronz.ai is authenticated in this coding-agent process through "
            f"{status_subject}. {precedence} {restart} to use OAuth."
        )
    if action == "logout":
        lead = (
            "The saved OAuth grant was removed, but "
            if removed
            else "No saved OAuth grant remains, but "
        )
        return (
            f"{lead}this coding-agent process is still authenticated through "
            f"{status_subject}. {restart} to finish signing out."
        )
    raise ValueError(f"unknown environment override action: {action}")


def _issuer_key() -> str:
    return hashlib.sha256(base_url().encode("utf-8")).hexdigest()[:24]


def _config_dir() -> Path:
    override = os.environ.get("NEURONZAI_CREDENTIALS_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "Neuronz.ai"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Neuronz.ai"
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "neuronzai"


def credential_file() -> Path:
    return _config_dir() / f"oauth-{_issuer_key()}.json"


def marker_file() -> Path:
    return _config_dir() / f"oauth-{_issuer_key()}.state"


def _lock_file() -> Path:
    return _config_dir() / f"oauth-{_issuer_key()}.lock"


def _ensure_private_dir() -> None:
    directory = _config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            directory.chmod(0o700)


@contextlib.contextmanager
def _credential_lock(*, timeout: float | None = None):
    """Serialize refresh/write operations across concurrent hook processes."""
    _ensure_private_dir()
    path = _lock_file()
    fh = open(path, "a+b")
    locked = False
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if timeout is None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            else:
                deadline = time.monotonic() + max(timeout, 0)
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise CredentialBusy("Neuronz.ai credentials are busy.") from None
                        time.sleep(0.05)
        else:
            import fcntl

            if timeout is None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                locked = True
            else:
                deadline = time.monotonic() + max(timeout, 0)
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise CredentialBusy("Neuronz.ai credentials are busy.") from None
                        time.sleep(0.05)
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            with contextlib.suppress(OSError):
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        elif locked:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _run(command: list[str], *, stdin: str = "", timeout: float = 5) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )


def _requested_backend() -> str:
    value = (os.environ.get("NEURONZAI_CREDENTIAL_STORE") or "auto").strip().lower()
    return value if value in {"auto", "keyring", "file"} else "auto"


def _keyring_backend() -> str | None:
    if _requested_backend() == "file":
        return None
    system = platform.system().lower()
    if system == "linux" and shutil.which("secret-tool"):
        # Secret Service needs a user session bus. Without one, secret-tool may
        # block or fail and the permission-restricted file is the reliable fallback.
        if os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("XDG_RUNTIME_DIR"):
            return "secret-tool"
    if system == "darwin" and shutil.which("security"):
        return "security"
    if system == "windows" and (shutil.which("powershell") or shutil.which("pwsh")):
        return "dpapi"
    return None


def _keyring_account() -> str:
    return f"{getpass.getuser()}:{_issuer_key()}"


def _read_keyring(backend: str, *, timeout: float = 5) -> str | None:
    account = _keyring_account()
    try:
        if backend == "secret-tool":
            result = _run(
                ["secret-tool", "lookup", "service", KEYRING_SERVICE, "account", account],
                timeout=timeout,
            )
            return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
        if backend == "security":
            result = _run(
                ["security", "find-generic-password", "-s", KEYRING_SERVICE, "-a", account, "-w"],
                timeout=timeout,
            )
            return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
        if backend == "dpapi":
            path = _config_dir() / f"oauth-{_issuer_key()}.dpapi"
            if not path.is_file():
                return None
            shell = shutil.which("powershell") or shutil.which("pwsh")
            script = (
                "$s=Get-Content -Raw -LiteralPath $args[0] | ConvertTo-SecureString;"
                "$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);"
                "try{[Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}"
                "finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}"
            )
            result = _run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
                timeout=timeout,
            )
            return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _write_keyring(backend: str, value: str) -> bool:
    account = _keyring_account()
    try:
        if backend == "secret-tool":
            result = _run(
                [
                    "secret-tool",
                    "store",
                    "--label=Neuronz.ai OAuth",
                    "service",
                    KEYRING_SERVICE,
                    "account",
                    account,
                ],
                stdin=value,
            )
            return result.returncode == 0
        if backend == "security":
            # `security` has no stdin password flag. A short-lived argv value is
            # preferable to writing plaintext on disk; stdout/stderr stay suppressed.
            result = _run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    KEYRING_SERVICE,
                    "-a",
                    account,
                    "-w",
                    value,
                ]
            )
            return result.returncode == 0
        if backend == "dpapi":
            shell = shutil.which("powershell") or shutil.which("pwsh")
            script = "$input | ConvertTo-SecureString -AsPlainText -Force | ConvertFrom-SecureString"
            result = _run([shell, "-NoProfile", "-NonInteractive", "-Command", script], stdin=value)
            if result.returncode != 0 or not result.stdout.strip():
                return False
            _atomic_private_write(
                _config_dir() / f"oauth-{_issuer_key()}.dpapi", result.stdout.strip() + "\n"
            )
            return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def _delete_keyring(backend: str) -> None:
    account = _keyring_account()
    try:
        if backend == "secret-tool":
            _run(["secret-tool", "clear", "service", KEYRING_SERVICE, "account", account])
        elif backend == "security":
            _run(["security", "delete-generic-password", "-s", KEYRING_SERVICE, "-a", account])
        elif backend == "dpapi":
            with contextlib.suppress(OSError):
                (_config_dir() / f"oauth-{_issuer_key()}.dpapi").unlink()
    except (OSError, subprocess.SubprocessError):
        pass


def _atomic_private_write(path: Path, value: str) -> None:
    _ensure_private_dir()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)


def _read_state() -> dict:
    try:
        return json.loads(marker_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _write_marker(backend: str) -> None:
    _atomic_private_write(
        marker_file(),
        json.dumps({"version": STORE_VERSION, "backend": backend, "revision": time.time_ns()}) + "\n",
    )


def marker_revision() -> tuple[int, int]:
    """Non-secret fingerprint used by the MCP proxy to announce auth changes."""
    try:
        stat = marker_file().stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return (0, 0)


def _load_record(*, keyring_timeout: float = 5) -> dict | None:
    state_backend = str(_read_state().get("backend") or "")
    candidates: list[str] = []
    if state_backend in {"secret-tool", "security", "dpapi"}:
        candidates.append(state_backend)
    detected = _keyring_backend()
    if detected and detected not in candidates:
        candidates.append(detected)

    raw = None
    for backend in candidates:
        raw = _read_keyring(backend, timeout=keyring_timeout)
        if raw:
            break
    if not raw:
        try:
            raw = credential_file().read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict) or record.get("version") != STORE_VERSION:
        return None
    if str(record.get("issuer") or "").rstrip("/") != base_url():
        return None
    required = ("client_id", "access_token", "refresh_token", "token_endpoint")
    if any(not isinstance(record.get(key), str) or not record.get(key) for key in required):
        return None
    return record


def _save_record(record: dict, *, notify: bool = True) -> str:
    value = json.dumps(record, separators=(",", ":"), sort_keys=True)
    backend = _keyring_backend()
    if backend and _write_keyring(backend, value):
        with contextlib.suppress(OSError):
            credential_file().unlink()
        if notify:
            _write_marker(backend)
        return backend
    if _requested_backend() == "keyring":
        raise AuthError("The requested OS keyring is unavailable; credentials were not saved.")
    _atomic_private_write(credential_file(), value + "\n")
    if notify:
        _write_marker("file")
    return "file"


def _clear_local() -> bool:
    existed = _load_record() is not None
    backends = {str(_read_state().get("backend") or ""), _keyring_backend() or ""}
    for backend in backends & {"secret-tool", "security", "dpapi"}:
        _delete_keyring(backend)
    for path in (credential_file(), _config_dir() / f"oauth-{_issuer_key()}.dpapi"):
        with contextlib.suppress(OSError):
            path.unlink()
    _write_marker("none")
    return existed


def _same_origin(url: str, issuer: str) -> bool:
    left = urllib.parse.urlsplit(url)
    right = urllib.parse.urlsplit(issuer)
    return left.scheme == right.scheme and left.netloc == right.netloc


def _validated_endpoint(metadata: dict, key: str, issuer: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise AuthError(f"OAuth discovery did not provide {key}.")
    if not _same_origin(value, issuer):
        raise AuthError(f"OAuth discovery returned an unsafe cross-origin {key}.")
    return value


def _request_json(
    url: str,
    *,
    method: str = "GET",
    json_body: dict | None = None,
    form_body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 15,
) -> dict:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        safe_detail = ""
        with contextlib.suppress(Exception):
            payload = json.loads(error.read())
            safe_detail = str(payload.get("error_description") or payload.get("error") or "")
        suffix = f": {safe_detail}" if safe_detail else ""
        raise AuthError(f"OAuth request failed ({error.code}){suffix}.") from None
    except (OSError, urllib.error.URLError) as error:
        raise AuthError(f"Could not reach the Neuronz.ai OAuth server: {error}.") from None
    try:
        result = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        raise AuthError("The Neuronz.ai OAuth server returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise AuthError("The Neuronz.ai OAuth server returned an invalid response.")
    return result


def discover() -> dict:
    issuer = base_url()
    metadata = _request_json(f"{issuer}/.well-known/oauth-authorization-server")
    discovered_issuer = str(metadata.get("issuer") or "").rstrip("/")
    if discovered_issuer != issuer:
        raise AuthError("OAuth discovery issuer does not match the configured Neuronz.ai URL.")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        _validated_endpoint(metadata, key, issuer)
    return metadata


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def pending_file() -> Path:
    return _config_dir() / f"oauth-{_issuer_key()}.pending.json"


def _browser_available(browser_opener=None) -> bool:
    """Whether a browser that can COMPLETE an OAuth redirect exists here.

    An injected opener (tests, or a host that knows better) is always trusted.

    webbrowser.get() succeeding is NOT enough on its own. On Linux/BSD the
    stdlib registers the console browsers (www-browser, links, elinks, lynx,
    w3m) whenever TERM is set, with no display required, while GUI browsers are
    gated behind DISPLAY / WAYLAND_DISPLAY. So on exactly the headless boxes
    this fallback exists for, an installed w3m would otherwise look like a
    browser, send us down the loopback lane, and block until the 300s timeout
    waiting for a callback a text browser can never deliver. Require a display
    server anywhere that is not macOS or Windows, both of which always have a
    real browser.
    """
    if browser_opener is not None:
        return True
    if os.environ.get(FORCE_NO_BROWSER_ENV):
        return False
    if sys.platform not in {"darwin", "win32"} and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    try:
        webbrowser.get()
    except Exception:
        return False
    return True


def _save_pending(pending: dict) -> None:
    """Persist an in-flight out-of-band login between the two CLI invocations.

    Holds the PKCE verifier, so it is written 0600 into the same private
    directory as the credential store and cleared as soon as it is redeemed.
    """
    _ensure_private_dir()
    path = pending_file()
    path.write_text(json.dumps(pending), encoding="utf-8")
    if os.name != "nt":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def _load_pending() -> dict | None:
    try:
        raw = pending_file().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        pending = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(pending, dict):
        return None
    if int(pending.get("expires_at") or 0) <= int(time.time()):
        clear_pending()
        return None
    return pending


def clear_pending() -> None:
    with contextlib.suppress(OSError):
        pending_file().unlink()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    expected_path = "/oauth/callback"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != self.expected_path:
            self.send_error(404)
            return
        values = urllib.parse.parse_qs(parsed.query)
        payload = {key: items[0] for key, items in values.items() if items}
        with contextlib.suppress(queue.Full):
            self.result_queue.put_nowait(payload)
        body = (
            "<!doctype html><meta charset=utf-8><title>Neuronz.ai connected</title>"
            "<style>body{font:16px system-ui;margin:3rem;max-width:42rem}"
            "h1{color:#6d28d9}</style><h1>Neuronz.ai is connected</h1>"
            "<p>You can close this tab and return to your coding session.</p>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def _register_and_authorize(*, metadata: dict, issuer: str, redirect_uri: str) -> dict:
    """Register a fresh DCR client for this login and build its authorization URL."""
    registration_endpoint = _validated_endpoint(metadata, "registration_endpoint", issuer)
    registered = _request_json(
        registration_endpoint,
        method="POST",
        json_body={
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Neuronz.ai coding-agent plugin",
            "scope": OAUTH_SCOPE,
            "software_id": "neuronzai-plugin",
        },
    )
    client_id = registered.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise AuthError("OAuth client registration did not return a client_id.")

    verifier, challenge = _pkce_pair()
    # HEX, not token_urlsafe: the user pastes "<state>.<code>" as one string into
    # `auth_cli.py login --code <pasted>`, and token_urlsafe's alphabet includes
    # "-". A state beginning with a dash makes argparse read the value as another
    # option and exit 2 ("argument --code: expected one argument"), which killed
    # ~1.6% of browserless sign-ins (1/64 — one alphabet symbol in 64). Shell
    # quoting does not help: the dash survives quoting and argparse rejects it on
    # the value side. token_hex is [0-9a-f], so it can never be option-shaped, and
    # it still never contains the "." that PAIRING_SEPARATOR splits on.
    # 32 bytes keeps the previous 256 bits of entropy — token_hex(24) would have
    # quietly dropped it to 192 while fixing the dash.
    state = secrets.token_hex(32)
    authorization_endpoint = _validated_endpoint(metadata, "authorization_endpoint", issuer)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": OAUTH_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {
        "client_id": client_id,
        "verifier": verifier,
        "state": state,
        "authorization_url": authorization_endpoint + "?" + urllib.parse.urlencode(params),
    }


def _exchange_code(
    *,
    metadata: dict,
    issuer: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    code: str,
) -> dict:
    """Trade an authorization code for tokens.

    Split from storage so a caller can tell the two apart: once this returns, the
    authorization code is SPENT, whether or not the local save then succeeds.
    """
    token_endpoint = _validated_endpoint(metadata, "token_endpoint", issuer)
    token = _request_json(
        token_endpoint,
        method="POST",
        form_body={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": verifier,
        },
    )
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise AuthError("Neuronz.ai sign-in did not return refreshable credentials.")
    return {"token": token, "token_endpoint": token_endpoint}


def _store_grant(
    *,
    metadata: dict,
    issuer: str,
    client_id: str,
    token: dict,
    token_endpoint: str,
    previous_record: dict | None,
) -> dict:
    """Persist an exchanged grant and retire the one it replaces."""
    access = str(token["access_token"])
    refresh = str(token["refresh_token"])
    expires_in = int(token.get("expires_in") or 3600)
    revocation = metadata.get("revocation_endpoint")
    if not isinstance(revocation, str) or not _same_origin(revocation, issuer):
        revocation = f"{issuer}/api/auth/mcp/revoke"
    record = {
        "version": STORE_VERSION,
        "issuer": issuer,
        "client_id": client_id,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": int(time.time()) + max(expires_in, 1),
        "scope": str(token.get("scope") or OAUTH_SCOPE),
        "token_endpoint": token_endpoint,
        "revocation_endpoint": revocation,
    }
    try:
        with _credential_lock():
            backend = _save_record(record)
    except Exception:
        # The replacement cannot be recovered locally, so retire that new
        # DCR client best-effort while leaving the prior stored grant valid.
        _revoke_record(record)
        raise
    # Only retire the prior grant after the replacement is safely stored.
    # If an explicitly requested keyring is unavailable, _save_record raises
    # and the user's still-working previous login remains untouched.
    if previous_record:
        _revoke_record(previous_record)
    result = {
        "authenticated": True,
        "source": "oauth",
        "storage": backend,
        "issuer": issuer,
    }
    environment_variables = environment_token_names()
    if environment_variables:
        result["environment_override"] = True
        result["environment_variables"] = environment_variables
        result["message"] = environment_override_message("login", environment_variables)
    return result


def _exchange_and_store(
    *,
    metadata: dict,
    issuer: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    code: str,
    previous_record: dict | None,
) -> dict:
    """Exchange then store, for the browser lane where nothing sits in between."""
    exchanged = _exchange_code(
        metadata=metadata,
        issuer=issuer,
        client_id=client_id,
        redirect_uri=redirect_uri,
        verifier=verifier,
        code=code,
    )
    return _store_grant(
        metadata=metadata,
        issuer=issuer,
        client_id=client_id,
        token=exchanged["token"],
        token_endpoint=exchanged["token_endpoint"],
        previous_record=previous_record,
    )


def _login_begin_oob(*, issuer: str) -> dict:
    """Start a browserless login: print-a-URL now, paste-a-code later.

    Returns non-secret metadata only; the PKCE verifier stays in the 0600
    pending file for login_complete() to redeem.
    """
    metadata = discover()
    redirect_uri = f"{issuer}{OOB_REDIRECT_PATH}"
    started = _register_and_authorize(metadata=metadata, issuer=issuer, redirect_uri=redirect_uri)
    _save_pending(
        {
            "version": STORE_VERSION,
            "issuer": issuer,
            "client_id": started["client_id"],
            "redirect_uri": redirect_uri,
            "verifier": started["verifier"],
            "state": started["state"],
            "expires_at": int(time.time()) + PENDING_TTL_SECONDS,
        }
    )
    return {
        "authenticated": False,
        "pending": True,
        "source": "oauth",
        "issuer": issuer,
        "authorization_url": started["authorization_url"],
        "expires_in": PENDING_TTL_SECONDS,
    }


def login_complete(pairing: str) -> dict:
    """Finish a browserless login from the string shown on the completion page."""
    pending = _load_pending()
    if not pending:
        raise AuthError(
            "No Neuronz.ai sign-in is waiting to be completed, or it expired. "
            "Start the login again."
        )
    state, separator, code = pairing.strip().partition(PAIRING_SEPARATOR)
    if not separator or not code:
        raise AuthError(
            "That does not look like a Neuronz.ai sign-in code. Copy the whole "
            "string shown on the sign-in page."
        )
    if not secrets.compare_digest(state, str(pending.get("state") or "")):
        raise AuthError("Neuronz.ai sign-in returned an invalid state value.")

    issuer = base_url()
    # Defence in depth against a tampered or hand-copied pending file. Normal
    # server-switching cannot reach this: pending_file() is itself keyed by
    # _issuer_key(), so pointing NEURONZAI_URL elsewhere mid-login just misses
    # the file above and reports that nothing is waiting.
    if str(pending.get("issuer") or "") != issuer:
        clear_pending()
        raise AuthError("The waiting Neuronz.ai sign-in was for a different server.")

    with _credential_lock():
        previous_record = _load_record()
    metadata = discover()
    exchanged = _exchange_code(
        metadata=metadata,
        issuer=issuer,
        client_id=str(pending["client_id"]),
        redirect_uri=str(pending["redirect_uri"]),
        verifier=str(pending["verifier"]),
        code=code,
    )
    # The code is spent now, so this pending record can never be redeemed again.
    # Drop it BEFORE the local save, which can fail (no keyring) and would
    # otherwise strand a file holding a consumed code until its own TTL.
    clear_pending()
    return _store_grant(
        metadata=metadata,
        issuer=issuer,
        client_id=str(pending["client_id"]),
        token=exchanged["token"],
        token_endpoint=exchanged["token_endpoint"],
        previous_record=previous_record,
    )


def login(*, timeout: float = 300, browser_opener=None, no_browser: bool = False) -> dict:
    """Run browser OAuth and persist the resulting grant.

    Returns only non-secret status metadata.  Callers must never expose the
    internal record returned by the token endpoint.
    """
    issuer = base_url()
    # Preserve the working login until the replacement grant is ready. Once it
    # is, revoke this prior DCR client before overwriting the only local pointer
    # to it, so repeated explicit logins cannot accumulate abandoned grants.
    with _credential_lock():
        previous_record = _load_record()
    parsed = urllib.parse.urlsplit(issuer)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AuthError("OAuth requires HTTPS except for a local development server.")

    # No browser here means no loopback the user's browser could ever reach, so
    # bind nothing and hand back a URL they can open anywhere (#237). The two
    # lanes cannot race: redirect_uri is fixed when the authorization URL is
    # built and is bound into the code exchange, so the choice is made up front.
    if no_browser or not _browser_available(browser_opener):
        return _login_begin_oob(issuer=issuer)

    callback_queue: queue.Queue = queue.Queue(maxsize=1)
    handler = type("OAuthCallbackHandler", (_CallbackHandler,), {"result_queue": callback_queue})
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as error:
        raise AuthError(f"Could not open a local OAuth callback listener: {error}.") from None
    server.timeout = 0.5
    port = int(server.server_address[1])
    redirect_uri = f"http://127.0.0.1:{port}{handler.expected_path}"

    try:
        metadata = discover()
        started = _register_and_authorize(
            metadata=metadata,
            issuer=issuer,
            redirect_uri=redirect_uri,
        )
        client_id = started["client_id"]
        verifier = started["verifier"]
        state = started["state"]
        authorization_url = started["authorization_url"]
        opener = browser_opener or webbrowser.open
        try:
            opened = opener(authorization_url)
        except Exception as error:
            raise AuthError(f"Could not open the browser for Neuronz.ai sign-in: {error}.") from None
        if opened is False:
            raise AuthError("Could not open the browser for Neuronz.ai sign-in.")

        deadline = time.monotonic() + timeout
        callback = None
        while time.monotonic() < deadline:
            server.handle_request()
            try:
                callback = callback_queue.get_nowait()
                break
            except queue.Empty:
                continue
        if callback is None:
            raise AuthError("Neuronz.ai sign-in timed out before the browser returned.")
        if callback.get("state") != state:
            raise AuthError("Neuronz.ai sign-in returned an invalid state value.")
        if callback.get("error"):
            raise AuthError(
                "Neuronz.ai sign-in was not completed: "
                + str(callback.get("error_description") or callback.get("error"))
            )
        code = callback.get("code")
        if not code:
            raise AuthError("Neuronz.ai sign-in returned no authorization code.")

        return _exchange_and_store(
            metadata=metadata,
            issuer=issuer,
            client_id=client_id,
            redirect_uri=redirect_uri,
            verifier=verifier,
            code=code,
            previous_record=previous_record,
        )
    finally:
        server.server_close()


def _refresh(record: dict) -> dict:
    issuer = base_url()
    token_endpoint = str(record.get("token_endpoint") or "")
    if not _same_origin(token_endpoint, issuer):
        raise AuthError("Stored OAuth token endpoint no longer matches the configured server.")
    token = _request_json(
        token_endpoint,
        method="POST",
        form_body={
            "grant_type": "refresh_token",
            "client_id": record["client_id"],
            "refresh_token": record["refresh_token"],
        },
        timeout=REFRESH_TIMEOUT_SECONDS,
    )
    access = token.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError("Neuronz.ai did not return a refreshed access token.")
    refresh = token.get("refresh_token")
    updated = dict(record)
    updated["access_token"] = access
    if isinstance(refresh, str) and refresh:
        updated["refresh_token"] = refresh
    updated["expires_at"] = int(time.time()) + max(int(token.get("expires_in") or 3600), 1)
    updated["scope"] = str(token.get("scope") or updated.get("scope") or OAUTH_SCOPE)
    _save_record(updated, notify=False)
    return updated


def access_token(*, force_refresh: bool = False) -> str:
    """Return a usable bearer without ever printing it.

    Legacy environment credentials win deliberately so CI and emergency users
    can override a stored grant without modifying the credential store.
    """
    legacy = environment_token()
    if legacy:
        return legacy
    try:
        with _credential_lock(timeout=LOCK_WAIT_SECONDS):
            record = _load_record()
            if not record:
                return ""
            expires_at = int(record.get("expires_at") or 0)
            if force_refresh or expires_at <= int(time.time()) + REFRESH_SKEW_SECONDS:
                try:
                    record = _refresh(record)
                except AuthError:
                    return ""
            return str(record.get("access_token") or "")
    except CredentialBusy:
        return ""


def cached_access_token(*, keyring_timeout: float = 0.5) -> str:
    """Return an already-usable bearer without network refresh.

    The copied host status-line wrapper uses this bounded path so terminal
    rendering can never wait on an OAuth refresh request. Normal MCP/hook calls
    refresh the grant before it enters the skew window.
    """
    legacy = environment_token()
    if legacy:
        return legacy
    record = _load_record(keyring_timeout=keyring_timeout)
    if not record:
        return ""
    if int(record.get("expires_at") or 0) <= int(time.time()) + REFRESH_SKEW_SECONDS:
        return ""
    return str(record.get("access_token") or "")


def status(*, verify: bool = False) -> dict:
    environment_variables = environment_token_names()
    if environment_variables:
        return {
            "authenticated": True,
            "source": "environment",
            "issuer": base_url(),
            "environment_variables": environment_variables,
            "message": environment_override_message(
                "status",
                environment_variables,
            ),
        }
    record = _load_record()
    if not record:
        return {"authenticated": False, "source": "none", "issuer": base_url()}
    if verify and not access_token():
        return {
            "authenticated": False,
            "source": "oauth",
            "issuer": base_url(),
            "reauthentication_required": True,
            "storage": str(_read_state().get("backend") or "unknown"),
        }
    return {
        "authenticated": True,
        "source": "oauth",
        "issuer": base_url(),
        "expires_at": int(record.get("expires_at") or 0),
        "refreshable": bool(record.get("refresh_token")),
        "storage": str(_read_state().get("backend") or "unknown"),
    }


def _revoke_record(record: dict) -> bool:
    endpoint = str(record.get("revocation_endpoint") or "")
    if not endpoint or not _same_origin(endpoint, base_url()):
        return False
    try:
        _request_json(
            endpoint,
            method="POST",
            form_body={
                "token": record["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": record["client_id"],
            },
        )
        return True
    except AuthError:
        return False


def logout() -> dict:
    """Best-effort server revocation followed by unconditional local deletion."""
    environment_variables = environment_token_names()
    has_environment_token = bool(environment_variables)
    with _credential_lock():
        record = _load_record()
        revoked = _revoke_record(record) if record else False
        removed = _clear_local()
    result = {
        "authenticated": has_environment_token,
        "source": "environment" if has_environment_token else "none",
        "revoked": revoked,
        "removed": removed,
        "issuer": base_url(),
    }
    if has_environment_token:
        result["environment_variables"] = environment_variables
        result["message"] = environment_override_message(
            "logout",
            environment_variables,
            removed=removed,
        )
    return result
