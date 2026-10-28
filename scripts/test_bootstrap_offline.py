"""Real isolated-process checks: offline bootstrap never installs or connects."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv

import skill_runtime


class OfflineBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source_scripts = Path(__file__).resolve().parent

    def make_skill(self, name, dependency_version=None, *, with_runtime=True, repair=False):
        root = self.root / name
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        for filename in ("review.py", "skill_runtime.py", "report_diagnostics.py", "install_tokenizer.py"):
            shutil.copyfile(self.source_scripts / filename, scripts / filename)
        # Any accidental environment creation is observable before it can
        # install pip or contact the network.
        (scripts / "venv.py").write_text(
            "from pathlib import Path\nclass EnvBuilder:\n"
            " def __init__(self,*a,**k):pass\n"
            " def create(self,*a,**k):\n"
            "  Path(" + repr(str(root / "venv-created")) + ").write_text('unexpected')\n"
            "  raise RuntimeError('No implicit environment creation')\n", encoding="utf-8")
        if not with_runtime:
            return root
        venv.EnvBuilder(with_pip=False, system_site_packages=False).create(root / ".venv")
        site = (root / ".venv" / "Lib/site-packages" if os.name == "nt" else
                root / ".venv/lib" / ("python%d.%d" % sys.version_info[:2]) / "site-packages")
        site.mkdir(parents=True, exist_ok=True)
        versions = {"requests": "2.34.2", "tzdata": "2026.5", "json-repair": "0.64.2",
                    "tokenizers": "0.23.3", "Jinja2": "3.1.6"}
        if dependency_version is not None:
            versions["tenacity"] = dependency_version
        for distribution, version in versions.items():
            metadata = site / (distribution.replace("-", "_") + "-" + version + ".dist-info")
            metadata.mkdir()
            (metadata / "METADATA").write_text("Name: " + distribution + "\nVersion: " + version + "\n", encoding="utf-8")
        guard = ("from pathlib import Path\nimport socket\n"
                 "Path(" + repr(str(root / "network-guard-ready")) + ").write_text('active')\n"
                 "def blocked(*a,**k):\n"
                 " Path(" + repr(str(root / "network-used")) + ").write_text('unexpected')\n"
                 " raise RuntimeError('Network disabled in bootstrap regression')\n"
                 "socket.getaddrinfo=blocked\nsocket.socket.connect=blocked\nsocket.socket.connect_ex=blocked\n")
        (site / "sitecustomize.py").write_text(guard, encoding="utf-8")
        package = site / "pip"
        package.mkdir()
        (package / "__init__.py").touch()
        fake_pip = ("from pathlib import Path\n"
                    "Path(" + repr(str(root / "pip-used")) + ").write_text('attempted')\n"
                    "print('dependency bootstrap diagnostic')\n")
        if repair:
            fake_pip += ("p=Path(" + repr(str(site / "tenacity-9.2.1.dist-info")) + ")\n"
                         "p.mkdir(exist_ok=True)\n"
                         "(p/'METADATA').write_text('Name: tenacity\\nVersion: 9.2.1\\n')\n")
        else:
            fake_pip += "raise RuntimeError('No offline dependency installation permitted')\n"
        (package / "__main__.py").write_text(fake_pip, encoding="utf-8")
        return root

    def run_entry(self, root, filename, *arguments, use_source_python=False):
        executable = sys.executable if use_source_python else str(skill_runtime.runtime_python(root))
        return subprocess.run([executable, "-I", "-B", str(root / "scripts" / filename), *arguments],
                              cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def assert_no_install_or_network(self, root):
        self.assertFalse((root / "pip-used").exists())
        self.assertFalse((root / "network-used").exists())
        self.assertFalse((root / "venv-created").exists())
        self.assertTrue((root / "network-guard-ready").is_file())

    def test_real_offline_review_missing_or_wrong_dependency_never_installs(self):
        for label, version in (("missing", None), ("wrong", "0.0.0")):
            root = self.make_skill(label, version)
            for flag in ("--history", "--route-only", "--runtime-info", "--dry-run", "--help"):
                with self.subTest(state=label, flag=flag):
                    completed = self.run_entry(root, "review.py", flag)
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    self.assertIn("setup", completed.stderr)
                    self.assertEqual(completed.stdout, "")
                    self.assert_no_install_or_network(root)
                    self.assertFalse((root / "logs").exists())

    def test_real_missing_venv_never_creates_or_installs_it(self):
        root = self.make_skill("missing-venv", with_runtime=False)
        for flag in ("--history", "--route-only", "--runtime-info", "--dry-run"):
            with self.subTest(flag=flag):
                completed = self.run_entry(root, "review.py", flag, use_source_python=True)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("setup", completed.stderr)
                self.assertFalse((root / ".venv").exists())
                self.assertFalse((root / "venv-created").exists())
                self.assertFalse((root / "pip-used").exists())

    def test_diagnostics_and_local_installer_do_not_repair_dependencies(self):
        root = self.make_skill("offline-local-entry", "0.0.0")
        completed = self.run_entry(root, "report_diagnostics.py", "--help")
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("setup", completed.stderr)
        self.assert_no_install_or_network(root)
        installer = self.run_entry(root, "install_tokenizer.py", "--help")
        self.assertEqual(installer.returncode, 0, installer.stderr)
        self.assert_no_install_or_network(root)

    def test_live_bootstrap_can_repair_and_keeps_stdout_single_json(self):
        root = self.make_skill("live-repair", repair=True)
        probe = root / "scripts/live_probe.py"
        probe.write_text("import sys,json\nfrom pathlib import Path\n"
                         "sys.path.insert(0,str(Path(__file__).resolve().parent))\n"
                         "from skill_runtime import launch_in_runtime\n"
                         "launch_in_runtime(__file__,allow_install=True)\n"
                         "print(json.dumps({'status':'ready'}))\n", encoding="utf-8")
        completed = self.run_entry(root, "live_probe.py")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"status": "ready"})
        self.assertTrue((root / "pip-used").exists())
        self.assertFalse((root / "network-used").exists())
        self.assertIn("dependency bootstrap diagnostic", completed.stderr)


if __name__ == "__main__":
    unittest.main()
