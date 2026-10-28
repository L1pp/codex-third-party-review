"""Readiness and isolated npm setup for the official Harness runtime.

Only explicit setup or an authorized live entry may install dependencies. Node
itself is supplied by the host; this module never installs or changes it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SKILL_ROOT = Path(__file__).resolve().parent.parent
MIN_NODE_VERSION = (24, 9, 0)
NODE_ENV_KEYS = ("NODE_OPTIONS", "NODE_PATH", "NODE_REPL_EXTERNAL_MODULE")


class NodeRuntimeError(RuntimeError):
    """An actionable local diagnostic without credentials or provider data."""


def node_environment(environment=None):
    result = dict(os.environ if environment is None else environment)
    for name in NODE_ENV_KEYS:
        result.pop(name, None)
    # The existing API client did not inherit environment proxy routing.
    result["NODE_USE_ENV_PROXY"] = "0"
    return result


def node_executable(config=None):
    """Resolve an explicitly selected Node or the host PATH without a shell."""
    configured = (config or {}).get("node_executable")
    if configured is not None:
        if not isinstance(configured, str) or not configured.strip():
            raise NodeRuntimeError("node_executable must name a Node executable")
        candidate = Path(configured).expanduser()
        resolved = str(candidate.resolve()) if candidate.is_file() else shutil.which(configured)
    else:
        resolved = shutil.which("node")
    if not resolved or not Path(resolved).is_file():
        raise NodeRuntimeError("Node.js >=24.9.0 is required; install Node or set node_executable")
    return str(Path(resolved).resolve())


def _node_version(executable):
    try:
        completed = subprocess.run([executable, "--version"], env=node_environment(),
                                   capture_output=True, text=True, encoding="utf-8", timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise NodeRuntimeError("Node.js version could not be checked") from None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?", completed.stdout.strip())
    if completed.returncode or match is None:
        raise NodeRuntimeError("Node.js version could not be checked")
    if tuple(map(int, match.groups())) < MIN_NODE_VERSION:
        raise NodeRuntimeError("Node.js >=24.9.0 is required for the official Harness runtime")
    return completed.stdout.strip()


def _runtime_directory(root):
    root = Path(root).resolve()
    directory = (root / "runtime").resolve()
    if not directory.is_relative_to(root):
        raise NodeRuntimeError("Harness runtime directory must remain inside the Skill")
    return directory


def _manifest(root):
    try:
        manifest = json.loads((_runtime_directory(root) / "package.json").read_text(encoding="utf-8-sig"))
        dependencies = manifest["dependencies"]
        if not isinstance(dependencies, dict) or not dependencies:
            raise ValueError("dependencies")
        if not all(isinstance(name, str) and isinstance(version, str)
                   and re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version)
                   for name, version in dependencies.items()):
            raise ValueError("exact versions")
        return dependencies
    except (OSError, ValueError, TypeError, KeyError):
        raise NodeRuntimeError("Harness runtime package metadata is missing or invalid") from None


def check_node_runtime(root=SKILL_ROOT, config=None):
    """Inspect Node and package versions only; never install or call a model."""
    root = Path(root).resolve()
    executable = node_executable(config)
    version = _node_version(executable)
    expected = _manifest(root)
    bridge = _runtime_directory(root) / "bridge.mjs"
    if not bridge.is_file():
        raise NodeRuntimeError("Harness runtime entry is missing; update the Skill installation")
    try:
        completed = subprocess.run([executable, str(bridge), "--runtime-info"],
                                   cwd=root, env=node_environment(), capture_output=True,
                                   text=True, encoding="utf-8", timeout=30)
        value = json.loads(completed.stdout) if completed.returncode == 0 else None
        if not isinstance(value, dict) or not isinstance(value.get("dependencies"), dict):
            raise ValueError("runtime info")
        if any(value["dependencies"].get(name) != required for name, required in expected.items()):
            raise ValueError("versions")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise NodeRuntimeError("Harness runtime dependencies are incomplete; run scripts/skill_runtime.py setup") from None
    return {"available": True, "ready": True, "executable": executable,
            "version": version, "official_harness": value.get("officialHarness"),
            "dependencies": value["dependencies"]}


def node_runtime_info(root=SKILL_ROOT, config=None):
    """Return a read-only readiness report even when Node or packages are absent."""
    try:
        executable = node_executable(config)
    except NodeRuntimeError as error:
        return {"available": False, "ready": False, "error": str(error)}
    try:
        return check_node_runtime(root, config)
    except NodeRuntimeError as error:
        return {"available": True, "ready": False, "executable": executable, "error": str(error)}


def _npm_cli(executable):
    """Locate npm's JavaScript entry rather than invoking cmd/PowerShell shims."""
    node = Path(executable).resolve()
    candidates = [node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"]
    for name in ("npm", "npm.cmd"):
        found = shutil.which(name)
        if not found:
            continue
        path = Path(found).resolve()
        if path.suffix == ".js":
            candidates.append(path)
        candidates.append(path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js")
        # Some Unix distributions expose a JavaScript npm launcher without .js.
        if path.name == "npm" and path.is_file():
            try:
                with path.open(encoding="utf-8") as handle:
                    if handle.readline().startswith("#!/usr/bin/env node"):
                        candidates.append(path)
            except (OSError, UnicodeError):
                pass
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    raise NodeRuntimeError("npm is unavailable; install Node.js with npm before Skill setup")


def setup_node_runtime(root=SKILL_ROOT, config=None):
    """Install exact package dependencies inside this Skill's runtime directory."""
    root = Path(root).resolve()
    executable = node_executable(config)
    _node_version(executable)
    _manifest(root)
    try:
        return check_node_runtime(root, config)
    except NodeRuntimeError:
        pass
    npm = _npm_cli(executable)
    command = [executable, npm, "install", "--prefix", str(_runtime_directory(root)),
               "--ignore-scripts", "--no-audit", "--no-fund", "--package-lock=false", "--save-exact"]
    try:
        subprocess.run(command, cwd=root, env=node_environment(), stdout=sys.stderr,
                       check=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        raise NodeRuntimeError("Harness dependency installation failed; existing configuration and records were left unchanged") from None
    return check_node_runtime(root, config)


def ensure_node_runtime(root=SKILL_ROOT, *, allow_install=False, config=None):
    """Allow repair only for an explicitly authorized live or setup operation."""
    try:
        return check_node_runtime(root, config)
    except NodeRuntimeError:
        if not allow_install:
            raise
    return setup_node_runtime(root, config)
