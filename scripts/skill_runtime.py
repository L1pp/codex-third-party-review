#!/usr/bin/env python3
"""Provision and launch this Skill's isolated Python runtime; no model calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEPENDENCIES = ("requests", "tzdata", "json-repair", "tokenizers", "Jinja2")
PYTHON_ENV_KEYS = ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP")


def runtime_python(root=SKILL_ROOT):
    return Path(root) / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def clean_environment(root=SKILL_ROOT, environment=None):
    result = dict(os.environ if environment is None else environment)
    for key in PYTHON_ENV_KEYS:
        result.pop(key, None)
    result["VIRTUAL_ENV"] = str(Path(root) / ".venv")
    result["PATH"] = str(runtime_python(root).parent) + os.pathsep + result.get("PATH", "")
    return result


def using_skill_runtime(root=SKILL_ROOT):
    return Path(sys.prefix).resolve() == (Path(root) / ".venv").resolve()


def runtime_info(root=SKILL_ROOT):
    return {"python": sys.executable, "python_version": sys.version.split()[0],
            "skill_python": str(runtime_python(root)), "isolated": bool(sys.flags.isolated),
            "using_skill_runtime": using_skill_runtime(root)}


def check_runtime(root=SKILL_ROOT):
    """Check installed libraries and local native assets, without a model/network."""
    root = Path(root).resolve()
    code = ("import sys,json;from pathlib import Path;from importlib.metadata import version;"
            "sys.path.insert(0,sys.argv[1]);import context_budget;"
            "context_budget._TOKENIZER_DIR=Path(sys.argv[2]);"
            "d=context_budget.estimate_tokens([{'role':'user','content':'中文 token check'}],[],'deepseek-v4.1-flash-1017');"
            "assert d['method']=='deepseek_v4_official_tokenizer_template_plus_tool_format_estimate',d;"
            "print(json.dumps({'python':sys.executable,'isolated':bool(sys.flags.isolated),"
            "'dependencies':{x:version(x) for x in ['requests','tzdata','json-repair','tokenizers','Jinja2']},"
            "'tokenizer_method':d['method'],'sample_tokens':d['input_tokens']}))")
    completed = subprocess.run([str(runtime_python(root)), "-I", "-c", code,
                                str(root / "scripts"), str(root / "assets/deepseek_v4_tokenizer")],
                               env=clean_environment(root), text=True, encoding="utf-8", capture_output=True)
    if completed.returncode:
        raise RuntimeError("Skill runtime is incomplete; rerun scripts/skill_runtime.py setup with local tokenizer assets")
    record = json.loads(completed.stdout)
    if record["dependencies"]["json-repair"] != "0.64.2":
        raise RuntimeError("Skill runtime needs json-repair==0.64.2; rerun setup")
    return record


def setup_runtime(root=SKILL_ROOT, *, source_dir=None, source_zip=None, download_official=False):
    root = Path(root).resolve()
    destination = root / ".venv"
    if destination.exists() and not (destination / "pyvenv.cfg").is_file():
        raise RuntimeError("Existing .venv is not a Python virtual environment; it was left unchanged")
    if not runtime_python(root).is_file():
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(destination)
    subprocess.run([str(runtime_python(root)), "-I", "-m", "pip", "install", "-r", str(root / "requirements-tokenizer.txt")],
                   env=clean_environment(root), check=True)
    assets = root / "assets/deepseek_v4_tokenizer"
    if not all((assets / name).is_file() for name in ("tokenizer.json", "tokenizer_config.json")):
        from install_tokenizer import read_directory, read_zip_file, download_official as download, install_assets
        if source_dir is not None:
            content = read_directory(source_dir)
        elif source_zip is not None:
            content = read_zip_file(source_zip)
        elif download_official:
            content = download()
        else:
            raise RuntimeError("Tokenizer assets are missing; supply --source-dir, --source-zip or --download-official")
        # Existing complete installations are never overwritten by setup.
        install_assets(content, destination=assets, force=False)
    record = check_runtime(root)
    (destination / "skill-runtime.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def launch_in_runtime(entry_file, *, require_tokenizer=False):
    """CLI bootstrap, before importing project-installed third-party packages."""
    entry = Path(entry_file).resolve()
    root = entry.parent.parent
    executable = runtime_python(root)
    if not executable.is_file():
        print("Skill runtime is not prepared. Run: python \"%s\" setup --download-official" % (root / "scripts/skill_runtime.py"), file=sys.stderr)
        raise SystemExit(2)
    if not using_skill_runtime(root) or not sys.flags.isolated:
        command = [str(executable), "-I", str(entry), *sys.argv[1:]]
        environment = clean_environment(root)
        if os.name == "nt":
            # Windows CRT exec overlay can fail between different venv/Python
            # versions. A normal child process keeps stdio and its exit code.
            process = subprocess.Popen(command, env=environment)
            while True:
                try:
                    code = process.wait()
                    break
                except KeyboardInterrupt:
                    # Console interrupts reach both processes. Let the child
                    # finish writing its partial call records before exiting.
                    continue
            raise SystemExit(code)
        os.execve(str(executable), command, environment)
    if require_tokenizer:
        try:
            # These imports occur only after the isolated interpreter is active.
            from importlib.metadata import version
            if version("json-repair") != "0.64.2":
                raise RuntimeError("Unexpected JSON recovery dependency")
            from context_budget import _load_deepseek_assets
            _load_deepseek_assets()
        except Exception:
            print("Skill runtime dependencies/tokenizer are incomplete; rerun scripts/skill_runtime.py setup", file=sys.stderr)
            raise SystemExit(2) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "check", "test"))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source-dir", type=Path)
    source.add_argument("--source-zip", type=Path)
    source.add_argument("--download-official", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "test":
            return subprocess.call([str(runtime_python()), "-I", "-m", "unittest", "discover", "-s", str(SKILL_ROOT / "scripts"), "-p", "test_*.py", "-v"], env=clean_environment(), cwd=SKILL_ROOT)
        result = (setup_runtime(source_dir=args.source_dir, source_zip=args.source_zip,
                                download_official=args.download_official)
                  if args.action == "setup" else check_runtime())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print("Runtime setup/check failed. Existing config, credentials and review records were not modified.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
