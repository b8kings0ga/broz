#!/usr/bin/env python3
"""Broz deterministic prepare/watch/hot-deploy helper (Python stdlib only)."""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import fnmatch
import gzip
import hashlib
import http.client
import ipaddress
import io
import json
import os
import queue
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.parse
import uuid
import webbrowser
from pathlib import Path

API_DEFAULT = "https://mimir.broz.uk"
PUBLIC_LANE_DELAYS_FAST = (0.0, 0.04, 0.08, 0.12, 0.18, 0.26)
PUBLIC_LANE_DELAYS_PYTHON = (0.0, 0.04, 0.08, 0.12, 0.18, 0.26)
ACTIVATION_HEDGE_DELAY = 0.75
MAX_FILES = 4096
MAX_EXPANDED = 512 << 20
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".npm", ".aws", ".ssh"}
EXCLUDED_FILES = {".broz.json", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class BrozError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def fast_deployment_id(user_id: str, service_id: str, revision_id: str, request_id: str) -> str:
    identity = "\0".join((user_id, service_id, revision_id, request_id)).encode()
    return "dep_" + base64.urlsafe_b64encode(hashlib.sha256(identity).digest()[:18]).decode().rstrip("=")


def atomic_json(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def load_json(path: Path, required: bool = True) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        if required:
            raise BrozError(f"missing file: {path}")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise BrozError(f"invalid JSON file: {path}") from exc


def credential(path: Path) -> dict:
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise BrozError(f"credential file must have mode 0600: {path}")
    value = load_json(path)
    if not str(value.get("token", "")).strip():
        raise BrozError(f"credential profile has no token: {path}")
    return value


class API:
    def __init__(self, base: str, token: str, source_address: str = ""):
        self.base, self.token, self.source_address = base.rstrip("/"), token, source_address
        self.connections: dict[tuple[str, str], http.client.HTTPConnection] = {}

    def close_connection(self, key: tuple[str, str]) -> None:
        connection = self.connections.pop(key, None)
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()

    def connection(self, parsed: urllib.parse.SplitResult, timeout: float) -> tuple[tuple[str, str], http.client.HTTPConnection]:
        key = (parsed.scheme, parsed.netloc)
        connection = self.connections.get(key)
        if connection is None:
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout, source_address=(self.source_address, 0) if self.source_address else None)
            elif parsed.scheme == "http":
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout, source_address=(self.source_address, 0) if self.source_address else None)
            else:
                raise BrozError(f"unsupported Membership URL scheme: {parsed.scheme}")
            self.connections[key] = connection
        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)
        return key, connection

    def request(self, method: str, path: str, value=None, data: bytes | None = None, timeout: float = 185.0, headers: dict | None = None):
        request_headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}", "User-Agent": "broz-deploy/1.0"}
        if headers:
            request_headers.update(headers)
        if value is not None:
            data = json.dumps(value, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        target = path if path.startswith(("http://", "https://")) else self.base + path
        parsed = urllib.parse.urlsplit(target)
        request_path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        attempts = 2 if method in {"GET", "PUT"} or "Idempotency-Key" in request_headers else 1
        for attempt in range(attempts):
            key, connection = self.connection(parsed, timeout)
            try:
                connection.request(method, request_path, body=data, headers=request_headers)
                response = connection.getresponse()
                limit = (8 << 20) if 200 <= response.status < 300 else (64 << 10)
                body = response.read(limit)
                response_headers = dict(response.getheaders())
                if response.will_close:
                    self.close_connection(key)
                if not 200 <= response.status < 300:
                    try:
                        error = json.loads(body).get("error", "unknown")
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        error = "unknown"
                    raise BrozError(f"Membership {method} {path} returned HTTP {response.status}: {error}", response.status, str(error))
                if not body:
                    return {}, response.status, response_headers
                return json.loads(body), response.status, response_headers
            except BrozError:
                raise
            except (http.client.HTTPException, TimeoutError, OSError) as exc:
                self.close_connection(key)
                if attempt + 1 >= attempts:
                    raise BrozError(f"Membership request failed: {method} {path}") from exc
        raise BrozError(f"Membership request failed: {method} {path}")

    def activate_stream(self, path: str, value: dict, request_id: str, on_started) -> dict:
        parsed = urllib.parse.urlsplit(self.base + path)
        request_path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = {
            "Accept": "application/x-ndjson",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": request_id,
            "User-Agent": "broz-deploy/1.0",
        }
        key, connection = self.connection(parsed, 35)
        try:
            connection.request("POST", request_path, body=json.dumps(value, separators=(",", ":")).encode(), headers=headers)
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                body = response.read(64 << 10)
                try:
                    code = str(json.loads(body).get("error", "unknown"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    code = "unknown"
                raise BrozError(f"Membership POST {path} returned HTTP {response.status}: {code}", response.status, code)
            first_line = response.readline(1 << 20)
            if not first_line:
                raise BrozError("activation stream ended before deployment identity", code="activation_reconciling")
            first = json.loads(first_line)
            if not first.get("deployment_id"):
                raise BrozError("activation stream omitted deployment identity", code=str(first.get("error") or "activation_reconciling"))
            on_started(first)
            if first.get("status") == "activating":
                final_line = response.readline(1 << 20)
                if not final_line:
                    raise BrozError("activation stream ended before final state", code="activation_reconciling")
                final = json.loads(final_line)
            else:
                final = first
            response.read()
            if response.will_close:
                self.close_connection(key)
            if final.get("status") != "accessible":
                if final.get("status") in {"activating", "activation_running", "public_verifying", "reconciling"}:
                    raise BrozError(f"hot activation is {final.get('status')}", code="activation_reconciling")
                raise BrozError(f"hot activation is {final.get('status', 'unknown')}", int(final.get("http_status") or 0), str(final.get("error") or "hot_activation_failed"))
            if final.get("deployment_id") != first.get("deployment_id"):
                raise BrozError("activation stream changed deployment identity", code="activation_reconciling")
            return final
        except BrozError as exc:
            if exc.code != "activation_reconciling":
                raise
            self.close_connection(key)
            return self.reconcile_activation(path, value, request_id, on_started, exc)
        except (http.client.HTTPException, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.close_connection(key)
            return self.reconcile_activation(path, value, request_id, on_started, exc)

    def reconcile_activation(self, path: str, value: dict, request_id: str, on_started, cause: Exception) -> dict:
        """Resolve an uncertain response with the same idempotent operation."""
        deadline = time.monotonic() + 35
        last_status = "unknown"
        while time.monotonic() < deadline:
            try:
                result, _, _ = self.request(
                    "POST", path, value, timeout=35,
                    headers={"Idempotency-Key": request_id, "Accept": "application/json"},
                )
                deployment = str(result.get("deployment_id") or "")
                if deployment:
                    on_started(result)
                last_status = str(result.get("status") or "unknown")
                if last_status == "accessible":
                    return result
                if last_status == "cold_fallback":
                    raise BrozError("hot activation failed before switching", code="hot_activation_failed_cold_fallback_allowed")
            except BrozError as exc:
                if exc.code == "hot_activation_failed_cold_fallback_allowed":
                    raise
            time.sleep(0.1)
        raise BrozError(f"activation remains {last_status}; deployment reconciliation is required", code="activation_reconciling") from cause
def interface_address(interface: str) -> str:
    if not interface:
        return ""
    commands = []
    if shutil.which("ipconfig"):
        commands.append(["ipconfig", "getifaddr", interface])
    if shutil.which("ip"):
        commands.append(["ip", "-4", "-o", "addr", "show", "dev", interface])
    for command in commands:
        try:
            output = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3).stdout.strip()
            candidate = output if command[0] == "ipconfig" else output.split(" inet ", 1)[1].split("/", 1)[0]
            return str(ipaddress.ip_address(candidate))
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            continue
    raise BrozError(f"network interface has no IPv4 address: {interface}")


class Project:
    def __init__(self, path: str, profile: str, runtime: str, name: str, domain: str, binary: str, arch: str, open_browser: bool, direct_interface: str):
        self.root = Path(path).expanduser().resolve()
        if not self.root.is_dir():
            raise BrozError(f"project directory not found: {path}")
        self.state_path = self.root / ".broz.json"
        self.state = load_json(self.state_path, required=False)
        self.project_id = str(self.state.get("project_id") or uuid.uuid4())
        self.profile = profile or str(self.state.get("profile") or os.environ.get("BROZ_PROFILE", ""))
        requested_runtime = runtime if runtime != "auto" else str(self.state.get("runtime") or "auto")
        self.runtime = detect_runtime(self.root, requested_runtime, binary)
        self.arch = arch or str(self.state.get("arch") or "amd64")
        self.binary = Path(binary).expanduser().resolve() if binary else None
        self.name = name or safe_name(self.root.name)
        self.domain = domain or self.name
        self.open_browser = open_browser
        self.direct_interface = direct_interface
        self.source_address = interface_address(direct_interface)
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "broz"
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "broz"
        if self.profile:
            credential_path = config_root / "profiles" / f"{self.profile}.json"
        else:
            credential_path = config_root / "credentials" / f"{self.project_id}.json"
        if not credential_path.exists():
            raise BrozError("fast deploy requires a configured Membership profile; use the legacy guest cold deploy or select --profile")
        self.credential_path = credential_path
        auth = credential(credential_path)
        self.user_id = str(auth.get("user_id") or "")
        if not self.user_id:
            raise BrozError("fast deploy profile has no user_id")
        self.api = API(str(os.environ.get("BROZ_API_URL") or auth.get("api") or API_DEFAULT), str(auth["token"]), self.source_address)
        # Keep an independent connection ready for the idempotent activation
        # race. A single Cloudflare/TCP path occasionally adds >1s even while
        # the slot itself switches in ~100ms.
        self.activation_api = API(self.api.base, str(auth["token"]), self.source_address)
        self.cache_path = cache_root / "projects" / f"{self.project_id}.json"
        self.lock_path = cache_root / "locks" / f"{self.project_id}.lock"
        self.worker_path = cache_root / "workers" / f"{self.project_id}.json"
        self.socket_path = cache_root / "workers" / f"{self.project_id}.sock"
        # sockaddr_un is only 104 bytes on macOS. Keep the normal socket in the
        # private cache, but use a private short runtime directory when an XDG
        # cache path cannot be represented.
        if len(os.fsencode(self.socket_path)) >= 96:
            runtime_root = Path(tempfile.gettempdir()) / f"broz-{os.getuid()}"
            runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(runtime_root, 0o700)
            socket_name = hashlib.sha256(self.project_id.encode()).hexdigest()[:32] + ".sock"
            self.socket_path = runtime_root / socket_name
        self.log_path = cache_root / "logs" / f"{self.project_id}.log"
        self.public_connections: dict[tuple[str, str], http.client.HTTPConnection] = {}
        self.active_deployment_id = ""

    @contextlib.contextmanager
    def lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+", encoding="utf-8") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield

    def save_state(self, service: dict) -> None:
        value = {
            "version": 2, "project_id": self.project_id, "service_id": service["id"],
            "primary_hostname": service["hostname"], "hostnames": [service["hostname"]],
            "hostname": service["hostname"], "runtime": self.runtime, "arch": self.arch,
            "profile": self.profile, "fast_mode": True,
        }
        atomic_json(self.state_path, value, 0o600)
        self.state = value


def safe_name(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")[:40]
    if len(cleaned) < 3 or not cleaned[0].isalpha():
        cleaned = "app-" + secrets.token_hex(4)
    return cleaned


def detect_runtime(root: Path, requested: str, binary: str) -> str:
    if requested != "auto":
        return requested
    if binary:
        return "binary"
    if (root / "package.json").is_file() and ((root / "bun.lock").is_file() or (root / "bun.lockb").is_file()):
        return "bun"
    if (root / "pyproject.toml").is_file() and (root / "uv.lock").is_file():
        return "python"
    raise BrozError("cannot detect runtime; use --runtime")


def ignored(project: Project, relative: str, is_dir: bool, ignore_patterns: list[str]) -> bool:
    parts = Path(relative).parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = parts[-1]
    if name in EXCLUDED_FILES or name == ".brozignore" or name == ".env" or name.startswith(".env.") or name.endswith((".pyc", ".pyo", ".sock")):
        return True
    if any(fnmatch.fnmatch(relative, pattern) for pattern in ignore_patterns):
        return True
    if not is_dir and Path(name).suffix.lower() in SECRET_SUFFIXES:
        raise BrozError(f"refusing sensitive key file: {relative}")
    return False


def source_files(project: Project) -> list[tuple[str, Path, int]]:
    patterns: list[str] = []
    ignore_file = project.root / ".brozignore"
    if ignore_file.is_file():
        patterns = [line.strip() for line in ignore_file.read_text("utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    files: list[tuple[str, Path, int]] = []
    for directory, dirnames, filenames in os.walk(project.root, topdown=True, followlinks=False):
        base = Path(directory)
        dirnames[:] = sorted(name for name in dirnames if not ignored(project, (base / name).relative_to(project.root).as_posix(), True, patterns) and not (base / name).is_symlink())
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(project.root).as_posix()
            if ignored(project, relative, False, patterns):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            files.append((relative, path, 0o755 if info.st_mode & 0o111 else 0o644))
            if len(files) > MAX_FILES:
                raise BrozError("revision contains too many files")
    return files


def source_signature(project: Project) -> str:
    """Cheap watcher hint; prepare/deploy still freeze and hash every byte."""
    digest = hashlib.sha256()
    if project.binary is not None:
        info = project.binary.stat()
        digest.update(str(project.binary).encode())
        digest.update(f"\0{info.st_size}\0{info.st_mtime_ns}\0".encode())
        return digest.hexdigest()
    for relative, path, mode in source_files(project):
        info = path.stat()
        digest.update(relative.encode())
        digest.update(f"\0{mode:o}\0{info.st_size}\0{info.st_mtime_ns}\0".encode())
    return digest.hexdigest()


def command_version(command: str) -> str:
    try:
        return subprocess.run([command, "--version"], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def content_snapshot_digest(runtime_name: str, arch: str, dependency_digest: str, blobs: list[dict]) -> str:
    canonical = [{key: blob[key] for key in ("path", "mode", "content_sha256", "expanded_bytes")} for blob in blobs]
    body = {"runtime": runtime_name, "arch": arch, "dependency_digest": dependency_digest, "files": canonical}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fast_binary_snapshot_identity(project: Project) -> str:
    if project.binary is None or not project.binary.is_file():
        raise BrozError("binary runtime requires --binary FILE")
    with project.binary.open("rb") as stream:
        before = os.fstat(stream.fileno())
        content = stream.read()
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise BrozError("binary changed while freezing the deploy snapshot")
    header = content[:20]
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1 or int.from_bytes(header[18:20], "little") != 62:
        raise BrozError("binary must be an amd64 Linux ELF")
    blob = {"path": "service", "mode": 0o755, "content_sha256": "sha256:" + hashlib.sha256(content).hexdigest(), "expanded_bytes": len(content)}
    return content_snapshot_digest("binary", project.arch, "", [blob])


def prepare_snapshot(project: Project, temporary: Path) -> dict:
    if project.arch != "amd64":
        raise BrozError("v1 fast deploy supports only amd64")
    if project.runtime == "bun":
        package = load_json(project.root / "package.json")
        if not str(package.get("scripts", {}).get("start", "")).strip():
            raise BrozError("Bun requires scripts.start")
        lock = project.root / ("bun.lock" if (project.root / "bun.lock").is_file() else "bun.lockb")
        files = source_files(project)
        dependency_parts = [command_version("bun").encode(), lock.read_bytes(), (project.root / "package.json").read_bytes()]
    elif project.runtime == "python":
        if not (project.root / "pyproject.toml").is_file() or not (project.root / "uv.lock").is_file():
            raise BrozError("Python requires pyproject.toml and uv.lock")
        pyproject = tomllib.loads((project.root / "pyproject.toml").read_text("utf-8"))
        entry = str(pyproject.get("project", {}).get("scripts", {}).get("mimir-service", "")).strip()
        if not entry:
            raise BrozError("Python requires a mimir-service entry point")
        files = source_files(project)
        if any(relative == ".mim-python-entry.py" for relative, _, _ in files):
            raise BrozError("reserved Python launcher path exists: .mim-python-entry.py")
        module, separator, function = entry.partition(":")
        valid_part = lambda value: value and all(part.isidentifier() for part in value.split("."))
        if separator != ":" or not valid_part(module) or not function.isidentifier():
            raise BrozError("mimir-service must use the module:function form")
        launcher = temporary / "python-entry.py"
        launcher.write_text(
            "import sys\n"
            "sys.path.insert(0, 'src')\n"
            f"from {module} import {function} as _mim_entry\n"
            "_mim_entry()\n",
            encoding="utf-8",
        )
        files.append((".mim-python-entry.py", launcher, 0o644))
        dependency_parts = [command_version("uv").encode(), (project.root / "uv.lock").read_bytes(), (project.root / "pyproject.toml").read_bytes()]
    elif project.runtime == "binary":
        if project.binary is None or not project.binary.is_file():
            raise BrozError("binary runtime requires --binary FILE")
        header = project.binary.read_bytes()[:20]
        if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1 or int.from_bytes(header[18:20], "little") != 62:
            raise BrozError("binary must be an amd64 Linux ELF")
        files, dependency_parts = [("service", project.binary, 0o755)], []
    else:
        raise BrozError(f"unsupported runtime: {project.runtime}")
    blobs, expanded = [], 0
    for relative, path, mode in files:
        content = path.read_bytes()
        expanded += len(content)
        if expanded > MAX_EXPANDED:
            raise BrozError("revision exceeds expanded-size limit")
        compression, transport = "identity", content
        if project.runtime == "binary":
            zstd = shutil.which("zstd")
            if not zstd:
                raise BrozError("binary fast deploy requires the zstd command")
            target = temporary / "service.zst"
            with target.open("wb") as output:
                subprocess.run([zstd, "-q", "-c", str(path)], check=True, stdout=output, stderr=subprocess.DEVNULL)
            transport, compression = target.read_bytes(), "zstd"
        transport_digest, content_digest = hashlib.sha256(transport).hexdigest(), hashlib.sha256(content).hexdigest()
        blob_path = temporary / transport_digest
        blob_path.write_bytes(transport)
        blobs.append({"path": relative, "mode": mode, "transport_sha256": "sha256:" + transport_digest, "content_sha256": "sha256:" + content_digest, "compressed_bytes": len(transport), "expanded_bytes": len(content), "compression": compression, "_local": str(blob_path)})
    canonical = [{key: value for key, value in blob.items() if key != "_local"} for blob in blobs]
    manifest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    dependency = hashlib.sha256(b"\x00".join(dependency_parts)).hexdigest() if dependency_parts else ""
    dependency_digest = "sha256:" + dependency if dependency else ""
    return {"runtime": project.runtime, "arch": project.arch, "manifest_digest": "sha256:" + manifest, "content_snapshot_digest": content_snapshot_digest(project.runtime, project.arch, dependency_digest, blobs), "dependency_digest": dependency_digest, "blobs": blobs}


def cold_artifact(project: Project, temporary: Path) -> tuple[bytes, str]:
    stage = temporary / "cold"
    stage.mkdir()
    if project.runtime == "python":
        wheel_dir = temporary / "wheel"
        wheel_dir.mkdir()
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheel_dir)], cwd=project.root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wheels = list(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise BrozError("Python build must produce exactly one wheel")
        for source in (project.root / "pyproject.toml", project.root / "uv.lock", wheels[0]):
            shutil.copy2(source, stage / source.name)
    elif project.runtime == "binary":
        if project.binary is None:
            raise BrozError("binary runtime requires --binary FILE")
        shutil.copy2(project.binary, stage / "service")
        os.chmod(stage / "service", 0o755)
    else:
        for relative, source, mode in source_files(project):
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, mode)
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path in sorted(stage.rglob("*")):
                if not path.is_file():
                    continue
                info = archive.gettarinfo(str(path), path.relative_to(stage).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
    body = output.getvalue()
    return body, hashlib.sha256(body).hexdigest()


def service_by_id(project: Project) -> dict | None:
    service_id = str(project.state.get("service_id", ""))
    if not service_id:
        return None
    value, _, _ = project.api.request("GET", "/v1/services")
    service = next((item for item in value.get("items", []) if item.get("id") == service_id), None)
    if service is not None:
        project.active_deployment_id = str(service.get("active_deployment_id") or "")
    return service


def cold_deploy(project: Project, temporary: Path, ensure_service: bool = True) -> dict:
    package_started = monotonic_ms()
    artifact, digest = cold_artifact(project, temporary)
    package_ms = monotonic_ms() - package_started
    upload_started = monotonic_ms()
    uploaded = None
    for attempt in range(3):
        try:
            uploaded, _, _ = project.api.request("POST", f"/v1/artifacts?kind={project.runtime}&arch={project.arch}", data=artifact, headers={"Content-Type": "application/gzip", "Idempotency-Key": "artifact_" + digest}, timeout=120)
            break
        except BrozError as exc:
            if attempt == 2 or exc.code not in {"artifact_upload_incomplete", "artifact_storage_unavailable"}:
                raise
            time.sleep(0.05 * (attempt + 1))
    if uploaded is None:
        raise BrozError("artifact upload did not converge", code="artifact_upload_incomplete")
    upload_ms = monotonic_ms() - upload_started
    service = service_by_id(project)
    if service is None:
        service, _, _ = project.api.request("POST", "/v1/services", {"name": project.name, "artifact_id": uploaded["id"], "subdomain": project.domain})
        project.save_state(service)
    request_id = "broz_" + secrets.token_urlsafe(24).replace("-", "_")
    deploy_started = monotonic_ms()
    deployment, _, _ = project.api.request("POST", f"/v1/services/{service['id']}/deploy", {"artifact_id": uploaded["id"]}, headers={"Idempotency-Key": request_id})
    page_ms = wait_page(project, deployment["id"])
    return {"mode": "cold", "artifact_id": uploaded["id"], "service_id": service["id"], "deployment_id": deployment["id"], "public_url": "https://" + service["hostname"], "timings": {"package_ms": package_ms, "upload_ms": upload_ms, "deploy_to_ready_ms": monotonic_ms() - deploy_started, "page_ready_ms": page_ms}}


def ensure_service_and_slot(project: Project, temporary: Path) -> dict:
    service = service_by_id(project)
    if service is None or not service.get("active_deployment_id") or service.get("status") != "running":
        cold_deploy(project, temporary)
        service = service_by_id(project)
    if service is None:
        raise BrozError("service creation did not converge")
    value, _, _ = project.api.request("PUT", f"/v1/services/{service['id']}/fast-mode", {"enabled": True, "runtime": project.runtime, "arch": project.arch}, timeout=300)
    if not value.get("slot", {}).get("state") == "ready":
        raise BrozError("slot did not become ready")
    return service | {"_slot": value["slot"]}


def ensure_prepared(project: Project, force_slot_check: bool = False, force_reprepare: bool = False, persist: bool = True) -> dict:
    with tempfile.TemporaryDirectory(prefix="broz-prepare-") as directory:
        temporary = Path(directory)
        snapshot_started = monotonic_ms()
        snapshot = prepare_snapshot(project, temporary)
        snapshot_ms = monotonic_ms() - snapshot_started
        cached = load_json(project.cache_path, required=False)
        if not force_reprepare and not force_slot_check and cached.get("manifest_digest") == snapshot["manifest_digest"] and cached.get("prepared_receipt"):
            if cached.get("content_snapshot_digest") != snapshot["content_snapshot_digest"]:
                cached["content_snapshot_digest"] = snapshot["content_snapshot_digest"]
                atomic_json(project.cache_path, cached, 0o600)
            return cached | {"cache_hit": True, "snapshot_ms": snapshot_ms}
        if not force_slot_check and cached.get("service_id") and cached.get("slot_id") and cached.get("slot_incarnation_id") and cached.get("slot_generation"):
            slot = {"id": cached["slot_id"], "incarnation_id": cached["slot_incarnation_id"], "generation": cached["slot_generation"], "state": "ready"}
            service = {"id": cached["service_id"], "hostname": cached.get("hostname") or project.state.get("hostname"), "_slot": slot}
        else:
            service = ensure_service_and_slot(project, temporary)
            slot = service["_slot"]
        slot_matches = cached.get("slot_id") == slot.get("id") and cached.get("slot_incarnation_id") == slot.get("incarnation_id") and cached.get("slot_generation") == slot.get("generation")
        if not force_reprepare and cached.get("manifest_digest") == snapshot["manifest_digest"] and cached.get("prepared_receipt") and slot_matches:
            if cached.get("content_snapshot_digest") != snapshot["content_snapshot_digest"]:
                cached["content_snapshot_digest"] = snapshot["content_snapshot_digest"]
                atomic_json(project.cache_path, cached, 0o600)
            return cached | {"cache_hit": True, "snapshot_ms": snapshot_ms}
        # Local content_snapshot_digest is only a deploy-time shortcut. It is
        # not part of the signed RevisionTransportV2 wire contract.
        request = {key: snapshot[key] for key in ("runtime", "arch", "manifest_digest", "dependency_digest")}
        request["blobs"] = [{key: value for key, value in blob.items() if key != "_local"} for blob in snapshot["blobs"]]
        try:
            declared, _, _ = project.api.request("POST", f"/v1/services/{service['id']}/revisions", request)
        except BrozError as exc:
            if exc.code != "slot_not_ready":
                raise
            service = ensure_service_and_slot(project, temporary)
            slot = service["_slot"]
            declared, _, _ = project.api.request("POST", f"/v1/services/{service['id']}/revisions", request)
        revision = declared["revision_id"]
        local_by_digest = {blob["transport_sha256"].removeprefix("sha256:"): blob for blob in snapshot["blobs"]}
        upload_started = monotonic_ms()
        for target in declared.get("missing_blobs") or []:
            digest = str(target["transport_sha256"]).removeprefix("sha256:")
            blob = local_by_digest[digest]
            payload = Path(blob["_local"]).read_bytes()
            # Small source revisions must not pin the single watcher behind a
            # broken HTTP response for minutes. Large prebuilt binaries retain
            # the longer budget required by their intentionally asynchronous
            # compressed upload path.
            upload_timeout = 120 if len(payload) > (1 << 20) else 15
            for attempt in range(3):
                try:
                    project.api.request("PUT", target["upload_url"], data=payload, headers={"Content-Type": "application/octet-stream"}, timeout=upload_timeout)
                    break
                except BrozError as exc:
                    if attempt == 2 or exc.code not in {"fast_deploy_unavailable", "membership_runtime_unavailable"}:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        upload_ms = monotonic_ms() - upload_started
        prepare_started = monotonic_ms()
        prepared, _, _ = project.api.request("POST", f"/v1/services/{service['id']}/revisions/{revision}/prepare", {})
        prepare_ms = monotonic_ms() - prepare_started
        slot = prepared.get("slot") or slot
        cache = {"project_id": project.project_id, "service_id": service["id"], "hostname": service["hostname"], "runtime": project.runtime, "arch": project.arch, "slot_id": slot.get("id", ""), "slot_incarnation_id": slot.get("incarnation_id", ""), "slot_generation": slot.get("generation", 0), "manifest_digest": snapshot["manifest_digest"], "content_snapshot_digest": snapshot["content_snapshot_digest"], "dependency_digest": snapshot["dependency_digest"], "revision_id": revision, "prepared_receipt": prepared["prepared_receipt"], "prepared_at": prepared.get("prepared_at", ""), "prepare_metrics_ms": prepared.get("metrics_ms", {}), "snapshot_ms": snapshot_ms, "upload_ms": upload_ms, "prepare_api_ms": prepare_ms, "cache_hit": False}
        if persist:
            atomic_json(project.cache_path, cache, 0o600)
        return cache


def wait_page(project: Project, deployment_id: str, timeout: float = 120.0, stop: threading.Event | None = None, lane: int = 0) -> int:
    started = monotonic_ms()
    hostname = str(project.state.get("primary_hostname") or project.state.get("hostname"))
    scheme = os.environ.get("BROZ_PUBLIC_SCHEME", "https")
    parsed = urllib.parse.urlsplit(f"{scheme}://{hostname}")
    connection_key = (parsed.scheme, parsed.netloc, lane)
    deadline = time.monotonic() + timeout
    last_status, last_header, last_body = 0, "", b""
    while time.monotonic() < deadline and not (stop and stop.is_set()):
        connection = project.public_connections.get(connection_key)
        if connection is None:
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=5, source_address=(project.source_address, 0) if project.source_address else None)
            else:
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5, source_address=(project.source_address, 0) if project.source_address else None)
            project.public_connections[connection_key] = connection
        path = f"/?mim_deployment={urllib.parse.quote(deployment_id)}&broz={secrets.token_hex(4)}"
        try:
            connection.request("GET", path, headers={"Cache-Control": "no-cache", "User-Agent": "broz-deploy/1.0", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            last_status, last_header = response.status, response.getheader("X-Mim-Deployment", "")
            last_body = response.read(4 << 20)
            if response.will_close:
                project.public_connections.pop(connection_key, None)
                connection.close()
            if last_status == 200 and last_header == deployment_id:
                return monotonic_ms() - started
        except (http.client.HTTPException, TimeoutError, OSError):
            project.public_connections.pop(connection_key, None)
            with contextlib.suppress(OSError):
                connection.close()
        time.sleep(0.02)
    if stop and stop.is_set():
        raise BrozError("public page verification cancelled")
    preview = last_body[:256].decode("utf-8", "replace").replace("\n", " ")
    raise BrozError(f"public page did not become ready (HTTP {last_status or 'none'}, deployment header {last_header or 'missing'}, body {preview!r})")


def public_lane_delays(project: Project) -> tuple[float, ...]:
    return PUBLIC_LANE_DELAYS_PYTHON if project.runtime == "python" else PUBLIC_LANE_DELAYS_FAST


def warm_public_connections(project: Project) -> None:
    hostname = str(project.state.get("primary_hostname") or project.state.get("hostname") or "")
    if not hostname:
        return
    scheme = os.environ.get("BROZ_PUBLIC_SCHEME", "https")
    parsed = urllib.parse.urlsplit(f"{scheme}://{hostname}")

    def warm_lane(lane: int) -> None:
        key = (parsed.scheme, parsed.netloc, lane)
        if key in project.public_connections:
            return
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=5, source_address=(project.source_address, 0) if project.source_address else None)
        else:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5, source_address=(project.source_address, 0) if project.source_address else None)
        try:
            path = f"/?broz_warm={lane}&nonce={secrets.token_hex(4)}"
            connection.request("GET", path, headers={"Cache-Control": "no-cache", "User-Agent": "broz-deploy/1.0", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            response.read(4 << 20)
            if response.will_close:
                connection.close()
                return
            project.public_connections[key] = connection
        except (http.client.HTTPException, TimeoutError, OSError):
            with contextlib.suppress(OSError):
                connection.close()

    # Keep half the verification lanes fresh. Reusing every warmed TLS
    # connection is fastest in the common case, but gives the deploy no escape
    # hatch when that entire keep-alive cohort lands on a slow edge path.
    # Three warm lanes preserve the median; three delayed fresh lanes hedge the
    # tail without adding work to the activation critical path.
    warm_count = min(3, len(public_lane_delays(project)))
    lanes = [threading.Thread(target=warm_lane, args=(lane,), name=f"broz-public-warm-{lane}", daemon=True) for lane in range(warm_count)]
    for lane_thread in lanes:
        lane_thread.start()
    for lane_thread in lanes:
        lane_thread.join(timeout=6)


def warm_membership_connections(project: Project) -> None:
    def warm(api: API) -> None:
        with contextlib.suppress(BrozError):
            api.request("GET", "/readyz", timeout=6)

    lanes = [threading.Thread(target=warm, args=(api,), name=f"broz-api-warm-{lane}", daemon=True)
             for lane, api in enumerate((project.api, project.activation_api))]
    for lane in lanes:
        lane.start()
    for lane in lanes:
        lane.join(timeout=7)


def race_activation(project: Project, path: str, body: dict, request_id: str, on_started) -> tuple[dict, int, int]:
    outcomes: queue.Queue[tuple[str, object, int, int]] = queue.Queue()
    identity_seen = threading.Event()

    def lane(api: API, hedge_after: float) -> None:
        if hedge_after and identity_seen.wait(hedge_after):
            outcomes.put(("skip", None, 0, 0))
            return
        lane_started = monotonic_ms()
        first_seen = 0

        def started(value: dict) -> None:
            nonlocal first_seen
            if first_seen == 0:
                first_seen = monotonic_ms() - lane_started
            identity_seen.set()
            on_started(value)

        try:
            result = api.activate_stream(path, body, request_id, started)
            outcomes.put(("ok", result, first_seen, monotonic_ms() - lane_started))
        except BrozError as exc:
            outcomes.put(("error", exc, first_seen, monotonic_ms() - lane_started))

    lanes = [
        threading.Thread(target=lane, args=(project.api, 0.0), name="broz-activate-primary", daemon=True),
        # A warm Membership request normally exposes the deterministic
        # deployment identity in well under this budget. Only hedge a genuinely
        # delayed edge path; unconditional duplicates force the otherwise fast
        # request through the idempotent reconciliation path.
        threading.Thread(target=lane, args=(project.activation_api, ACTIVATION_HEDGE_DELAY), name="broz-activate-secondary", daemon=True),
    ]
    for thread in lanes:
        thread.start()
    errors = []
    for _ in lanes:
        kind, value, first_seen, complete = outcomes.get()
        if kind == "ok":
            return value, first_seen, complete
        if kind == "skip":
            continue
        errors.append(value)
    preferred = next((error for error in errors if isinstance(error, BrozError) and error.code == "prepared_receipt_mismatch"), errors[-1])
    raise preferred


def hot_deploy(project: Project, fallback: str) -> dict:
    started = monotonic_ms()
    with project.lock():
        snapshot_started = monotonic_ms()
        cached = load_json(project.cache_path, required=False)
        if project.runtime == "binary" and cached.get("prepared_receipt") and cached.get("content_snapshot_digest") == fast_binary_snapshot_identity(project):
            frozen = {"manifest_digest": cached.get("manifest_digest")}
        else:
            with tempfile.TemporaryDirectory(prefix="broz-freeze-") as directory:
                frozen = prepare_snapshot(project, Path(directory))
        snapshot_ms = monotonic_ms() - snapshot_started
        residual_started = monotonic_ms()
        if cached.get("manifest_digest") != frozen["manifest_digest"] or not cached.get("prepared_receipt"):
            cached = ensure_prepared(project)
        residual_ms = monotonic_ms() - residual_started
        service_id, revision = cached["service_id"], cached["revision_id"]
        request_id = "broz_" + secrets.token_urlsafe(24).replace("-", "_")
        expected_deployment = fast_deployment_id(project.user_id, service_id, revision, request_id)
        activate_started = monotonic_ms()
        page_started = 0
        page_result: dict[str, object] = {}
        page_stop = threading.Event()
        page_thread: threading.Thread | None = None
        activation_start_lock = threading.Lock()

        def activation_started(first: dict) -> None:
            nonlocal page_started, page_thread
            deployment = str(first["deployment_id"])
            with activation_start_lock:
                if page_thread is not None:
                    if deployment != expected_deployment:
                        raise BrozError("activation stream changed predicted deployment identity", code="activation_reconciling")
                    return
                page_started = monotonic_ms()

            def verify_public_page() -> None:
                outcomes: queue.Queue[tuple[str, object]] = queue.Queue()

                def verify_lane(lane: int, delay: float) -> None:
                    try:
                        delayed_ms = 0
                        if delay:
                            delay_started = monotonic_ms()
                            if page_stop.wait(delay):
                                raise BrozError("public page verification cancelled")
                            delayed_ms = monotonic_ms() - delay_started
                        outcomes.put(("ok", delayed_ms + wait_page(project, deployment, stop=page_stop, lane=lane)))
                    except BrozError as exc:
                        outcomes.put(("error", exc))

                lanes = [threading.Thread(target=verify_lane, args=(lane, delay), name=f"broz-public-page-{lane}", daemon=True) for lane, delay in enumerate(public_lane_delays(project))]
                for lane_thread in lanes:
                    lane_thread.start()
                errors = []
                for _ in lanes:
                    outcome, value = outcomes.get()
                    if outcome == "ok":
                        page_result["ms"] = value
                        page_stop.set()
                        break
                    errors.append(value)
                if "ms" not in page_result:
                    page_result["error"] = errors[-1]

            page_thread = threading.Thread(target=verify_public_page, name="broz-public-page", daemon=True)
            page_thread.start()

        activation_started({"deployment_id": expected_deployment})
        prepare_retries = 0
        while True:
            try:
                activation_path = f"/v1/services/{service_id}/revisions/{revision}/activate"
                activation_body = {"prepared_receipt": cached["prepared_receipt"]}
                begin_started = monotonic_ms()

                def stream_started(first: dict) -> None:
                    if str(first.get("deployment_id") or "") != expected_deployment:
                        raise BrozError("activation stream returned an unexpected deployment identity", code="activation_reconciling")
                    activation_started(first)

                result, begin_ms, complete_ms = race_activation(project, activation_path, activation_body, request_id, stream_started)
                break
            except BrozError as exc:
                if exc.code == "prepared_receipt_mismatch" and prepare_retries == 0:
                    retry_started = monotonic_ms()
                    cached = ensure_prepared(project, force_slot_check=True, force_reprepare=True)
                    residual_ms += monotonic_ms() - retry_started
                    service_id, revision = cached["service_id"], cached["revision_id"]
                    next_deployment = fast_deployment_id(project.user_id, service_id, revision, request_id)
                    if next_deployment != expected_deployment:
                        page_stop.set()
                        if page_thread is not None:
                            page_thread.join(timeout=1)
                        page_result = {}
                        page_stop = threading.Event()
                        page_thread = None
                        expected_deployment = next_deployment
                        activation_started({"deployment_id": expected_deployment})
                    prepare_retries += 1
                    continue
                page_stop.set()
                if page_thread is not None:
                    page_thread.join(timeout=1)
                if fallback != "cold" or exc.code not in {"hot_activation_failed_cold_fallback_allowed", "slot_not_ready", "revision_not_prepared", "fast_runtime_unavailable"}:
                    raise
                with tempfile.TemporaryDirectory(prefix="broz-cold-fallback-") as directory:
                    cold = cold_deploy(project, Path(directory))
                cold["mode"] = "cold_fallback"
                cold["hot_failure"] = exc.code or str(exc)
                cold["timings"]["total_ms"] = monotonic_ms() - started
                return cold
        activate_ms = monotonic_ms() - activate_started
        if page_thread is None:
            raise BrozError("activation did not reveal deployment identity")
        page_thread.join(timeout=120)
        if page_thread.is_alive():
            page_stop.set()
            raise BrozError("public page verification did not finish")
        if "error" in page_result:
            raise page_result["error"]
        page_ms = int(page_result["ms"])
        project.active_deployment_id = str(result["deployment_id"])
        public_url = str(result.get("public_url") or ("https://" + str(project.state.get("primary_hostname") or project.state.get("hostname") or "")))
        if public_url == "https://":
            raise BrozError("accessible activation omitted the public hostname", code="activation_reconciling")
        total_ms = monotonic_ms() - started
        return {"ok": True, "mode": "hot", "service_id": service_id, "revision_id": revision, "deployment_id": result["deployment_id"], "public_url": public_url, "prepare": {key: cached.get(key) for key in ("prepared_at", "prepare_metrics_ms", "snapshot_ms", "upload_ms", "prepare_api_ms", "cache_hit")}, "activation_metrics_ms": result.get("metrics_ms", {}), "timings": {"snapshot_ms": snapshot_ms, "residual_prepare_ms": residual_ms, "deployment_id_ready_ms": 0, "activation_response_first_ms": begin_ms, "activate_begin_api_ms": begin_ms, "activate_complete_api_ms": complete_ms, "activate_api_ms": activate_ms, "page_ready_ms": monotonic_ms() - page_started, "public_fetch_ms": page_ms, "total_ms": total_ms, "within_1s": total_ms < 1000}}


def worker_reply(connection: socket.socket, value: dict) -> None:
    encoded = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    # A CLI can be interrupted while the server-side idempotent activation is
    # still reconciling. Losing that local reader must not kill the persistent
    # prepare worker and discard its warmed connections.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
        connection.sendall(encoded)


def stop_background_worker(project: Project) -> bool:
    """Stop only the worker that proves ownership of this project's socket."""
    stopped = False
    try:
        info = project.socket_path.stat()
        if stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(project.socket_path))
                client.sendall(b'{"command":"shutdown"}\n')
                response = client.recv(4096)
                stopped = bool(json.loads(response).get("ok"))
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError, json.JSONDecodeError):
        pass
    if stopped:
        deadline = time.monotonic() + 2
        while project.socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    return stopped


def service_action(project: Project, command: str) -> dict:
    service_id = str(project.state.get("service_id") or "")
    if not service_id:
        raise BrozError("project has no Broz service")
    if command == "status":
        service = service_by_id(project)
        if service is None:
            raise BrozError("Broz service is not visible to this profile", status=404, code="service_not_found")
        return {"ok": True, "service": service}
    stop_background_worker(project)
    if command == "stop":
        value, _, _ = project.api.request("POST", f"/v1/services/{service_id}/stop", {})
        return {"ok": True, "service_id": service_id, "status": str(value.get("status") or "stopped")}
    if command != "delete":
        raise BrozError(f"unsupported service action: {command}")
    project.api.request("DELETE", f"/v1/services/{service_id}")
    for path in (project.state_path, project.cache_path, project.worker_path, project.log_path):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    # Named profiles belong to the account and survive project deletion. Only
    # a project-scoped guest credential is released with its deleted service.
    if not project.profile:
        with contextlib.suppress(FileNotFoundError):
            project.credential_path.unlink()
    return {"ok": True, "service_id": service_id, "status": "deleted", "local_state_removed": True}


def worker_request(project: Project, fallback: str) -> dict | None:
    """Ask the prepared-project worker to deploy; return None if it is absent."""
    try:
        info = project.socket_path.stat()
        if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(185)
            client.connect(str(project.socket_path))
            client.sendall(json.dumps({"command": "deploy", "fallback": fallback}).encode() + b"\n")
            response = bytearray()
            while len(response) <= 8 << 20 and not response.endswith(b"\n"):
                chunk = client.recv(64 << 10)
                if not chunk:
                    break
                response.extend(chunk)
        value = json.loads(response)
        if not value.get("ok"):
            raise BrozError(str(value.get("error") or "background worker deployment failed"), code=str(value.get("code") or ""))
        return value
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError, json.JSONDecodeError):
        return None


def watch(project: Project) -> None:
    project.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(FileNotFoundError):
        project.socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(project.socket_path))
    os.chmod(project.socket_path, 0o600)
    server.listen(4)
    server.settimeout(0.02)
    last, changed_at = "", 0.0
    next_scan = 0.0
    try:
        while True:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                connection = None
            if connection is not None:
                with connection:
                    connection.settimeout(2)
                    try:
                        request = json.loads(connection.recv(64 << 10))
                        if request.get("command") == "shutdown":
                            worker_reply(connection, {"ok": True, "stopped": True})
                            return
                        if request.get("command") != "deploy":
                            raise BrozError("unsupported worker command")
                        result = hot_deploy(project, str(request.get("fallback") or "cold"))
                        worker_reply(connection, result)
                    except BrozError as exc:
                        worker_reply(connection, {"ok": False, "error": str(exc), "code": exc.code})
                    except Exception as exc:  # never serialize exception details from network libraries
                        worker_reply(connection, {"ok": False, "error": f"worker failed: {type(exc).__name__}"})
                next_scan = time.monotonic() + 0.25
                continue
            now = time.monotonic()
            if now < next_scan:
                continue
            next_scan = now + 0.25
            try:
                signature = source_signature(project)
                if signature != last:
                    last, changed_at = signature, now
                if changed_at and now - changed_at >= 0.25:
                    with project.lock():
                        result = ensure_prepared(project, persist=False)
                    warm_membership_connections(project)
                    warm_public_connections(project)
                    if not result.get("cache_hit") and project.active_deployment_id:
                        with contextlib.suppress(BrozError):
                            wait_page(project, project.active_deployment_id, timeout=3)
                    # Publish local readiness only after all deploy-path
                    # connections have been warmed. The CLI must never observe
                    # a new receipt and then queue behind this single-threaded
                    # worker's remaining preparation work.
                    if not result.get("cache_hit"):
                        atomic_json(project.cache_path, {key: value for key, value in result.items() if key != "cache_hit"}, 0o600)
                    print(json.dumps({"event": "prepared", "revision_id": result["revision_id"], "manifest_digest": result["manifest_digest"]}), flush=True)
                    changed_at = 0.0
            except Exception as exc:  # fixed fields only; never print credentials or request headers
                failure = {"event": "prepare_failed", "error": type(exc).__name__}
                if isinstance(exc, BrozError):
                    failure.update({"code": exc.code or "unknown", "http_status": exc.status})
                print(json.dumps(failure), flush=True)
                # An unchanged latest snapshot must recover from a transient
                # Membership/slot failure without requiring another file edit.
                # Retain latest-wins semantics and retry after a bounded pause.
                changed_at = time.monotonic() + 1.0
    finally:
        server.close()
        with contextlib.suppress(FileNotFoundError):
            project.socket_path.unlink()


def start_watch(project: Project, raw_args: list[str]) -> dict:
    # The parent and detached watcher must share one identity even before the
    # first service create has populated the rest of .broz.json. Otherwise
    # each process can generate a UUID and wait on a different Unix socket.
    if str(project.state.get("project_id") or "") != project.project_id:
        initial_state = dict(project.state)
        initial_state.update({
            "version": 2,
            "project_id": project.project_id,
            "runtime": project.runtime,
            "arch": project.arch,
            "profile": project.profile,
            "fast_mode": True,
        })
        atomic_json(project.state_path, initial_state, 0o600)
        project.state = initial_state
    existing = load_json(project.worker_path, required=False)
    pid = int(existing.get("pid", 0) or 0)
    if pid:
        try:
            os.kill(pid, 0)
            if project.socket_path.exists() and stat.S_ISSOCK(project.socket_path.stat().st_mode):
                return {"ok": True, "watching": True, "pid": pid, "already_running": True}
            os.kill(pid, 15)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.02)
        except OSError:
            pass
    project.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [sys.executable, str(Path(__file__).resolve()), "_watch"] + raw_args
    with project.log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    atomic_json(project.worker_path, {"pid": process.pid, "project_id": project.project_id, "socket_path": str(project.socket_path), "started_at": time.time()}, 0o600)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if project.socket_path.exists():
            return {"ok": True, "watching": True, "pid": process.pid, "already_running": False}
        if process.poll() is not None:
            break
        time.sleep(0.02)
    raise BrozError("background prepare worker did not start")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="broz-deploy.sh")
    result.add_argument("command", choices=["prepare", "deploy", "status", "stop", "delete", "_watch"])
    result.add_argument("path")
    result.add_argument("--runtime", choices=["auto", "bun", "python", "binary"], default="auto")
    result.add_argument("--name", default="")
    result.add_argument("--domain", default="")
    result.add_argument("--binary", default="")
    result.add_argument("--arch", default="amd64")
    result.add_argument("--direct-interface", default="", help="opt in to binding Broz traffic to this interface, bypassing a VPN route")
    result.add_argument("--profile", default="")
    result.add_argument("--watch", action="store_true")
    result.add_argument("--once", action="store_true")
    result.add_argument("--open", dest="open_browser", action="store_true", default=True)
    result.add_argument("--no-open", dest="open_browser", action="store_false")
    result.add_argument("--fallback", choices=["cold", "never"], default="cold")
    result.add_argument("--yes", action="store_true")
    return result


def main(argv: list[str]) -> int:
    command_started = monotonic_ms()
    arguments = parser().parse_args(argv)
    parsed_at = monotonic_ms()
    project = Project(arguments.path, arguments.profile, arguments.runtime, arguments.name, arguments.domain, arguments.binary, arguments.arch, arguments.open_browser, arguments.direct_interface)
    project_ready_at = monotonic_ms()
    if arguments.command in {"status", "stop", "delete"}:
        if arguments.command == "delete" and not arguments.yes:
            raise BrozError("delete requires explicit --yes")
        print(json.dumps(service_action(project, arguments.command), separators=(",", ":")))
        return 0
    if arguments.command == "_watch":
        watch(project)
        return 0
    if arguments.command == "prepare" and arguments.watch and not arguments.once:
        forwarded = [arguments.path, "--runtime", arguments.runtime, "--arch", arguments.arch, "--no-open"]
        if arguments.direct_interface:
            forwarded.extend(("--direct-interface", arguments.direct_interface))
        for option, value in (("--profile", arguments.profile), ("--name", arguments.name), ("--domain", arguments.domain), ("--binary", arguments.binary)):
            if value:
                forwarded.extend((option, value))
        print(json.dumps(start_watch(project, forwarded), separators=(",", ":")))
        return 0
    if arguments.command == "prepare":
        with project.lock():
            prepared = ensure_prepared(project, force_slot_check=True)
        print(json.dumps({"ok": True, "state": "prepared", **{key: prepared.get(key) for key in ("service_id", "revision_id", "manifest_digest", "prepared_at", "prepare_metrics_ms", "cache_hit")}}, separators=(",", ":")))
        return 0
    worker_started = monotonic_ms()
    result = worker_request(project, arguments.fallback)
    if result is None:
        result = hot_deploy(project, arguments.fallback)
        result["worker"] = "direct_fallback"
    else:
        result["worker"] = "persistent"
    completed_at = monotonic_ms()
    timings = result.setdefault("timings", {})
    timings["cli_parse_ms"] = parsed_at - command_started
    timings["cli_project_ms"] = project_ready_at - parsed_at
    timings["worker_roundtrip_ms"] = completed_at - worker_started
    timings["command_total_ms"] = completed_at - command_started
    result["timings"]["within_1s"] = result["timings"]["command_total_ms"] < 1000
    if project.open_browser:
        webbrowser.open(result["public_url"], new=2)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrozError as error:
        print(f"broz: {error}", file=sys.stderr)
        raise SystemExit(1)
