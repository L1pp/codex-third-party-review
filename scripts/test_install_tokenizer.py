"""Offline tests for optional JSON-only tokenizer installation."""

from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import install_tokenizer as installer


def sample_assets(label="中文"):
    return {
        "tokenizer.json": json.dumps({"version": "1.0", "model": {"type": "BPE", "vocab": {label: 0}, "merges": []}}, ensure_ascii=False).encode("utf-8"),
        "tokenizer_config.json": json.dumps({"chat_template": "{{ bos_token }}{% for message in messages %}{{ message.content }}{% endfor %}", "bos_token": "<s>"}).encode("utf-8"),
    }


def zip_bytes(entries):
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return data.getvalue()


class OptionalTokenizerInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "assets"
        self.network = patch.object(installer, "urlopen", side_effect=AssertionError("Offline tests must not access the network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_source_directory_copies_only_two_json_files_and_preserves_unicode(self):
        source = self.root / "source"
        source.mkdir()
        assets = sample_assets()
        for name, data in assets.items():
            (source / name).write_bytes(data)
        (source / "demo.py").write_text("raise AssertionError('upstream code must never run')", encoding="utf-8")
        targets = installer.install_assets(installer.read_directory(source), self.destination)
        self.assertEqual({target.name for target in targets}, set(installer.FILENAMES))
        self.assertEqual({path.name for path in self.destination.iterdir()}, set(installer.FILENAMES))
        self.assertEqual({name: (self.destination / name).read_bytes() for name in installer.FILENAMES}, assets)
        installer.urlopen.assert_not_called()

    def test_missing_local_file_does_not_create_destination(self):
        source = self.root / "source"
        source.mkdir()
        (source / "tokenizer.json").write_bytes(sample_assets()["tokenizer.json"])
        with self.assertRaises(FileNotFoundError):
            installer.install_assets(installer.read_directory(source), self.destination)
        self.assertFalse(self.destination.exists())

    def test_invalid_second_json_does_not_replace_valid_existing_files(self):
        original = sample_assets("original")
        installer.install_assets(original, self.destination)
        replacement = sample_assets("replacement")
        replacement["tokenizer_config.json"] = b"not JSON"
        with self.assertRaises(installer.InstallError):
            installer.install_assets(replacement, self.destination, force=True)
        self.assertEqual({name: (self.destination / name).read_bytes() for name in installer.FILENAMES}, original)

    def test_empty_template_and_non_object_tokenizer_are_rejected_before_writes(self):
        for file_name, data in (("tokenizer_config.json", b'{"chat_template": "  "}'), ("tokenizer.json", b"[]"), ("tokenizer.json", b'{"model": null}')):
            with self.subTest(file_name=file_name, data=data):
                assets = sample_assets()
                assets[file_name] = data
                with self.assertRaises(installer.InstallError):
                    installer.install_assets(assets, self.destination)
                self.assertFalse(self.destination.exists())

    def test_existing_assets_require_explicit_force(self):
        original = sample_assets("original")
        replacement = sample_assets("replacement")
        installer.install_assets(original, self.destination)
        with self.assertRaises(installer.InstallError):
            installer.install_assets(replacement, self.destination)
        self.assertEqual((self.destination / "tokenizer.json").read_bytes(), original["tokenizer.json"])
        installer.install_assets(replacement, self.destination, force=True)
        self.assertEqual((self.destination / "tokenizer.json").read_bytes(), replacement["tokenizer.json"])

    def test_archive_installs_only_known_json_and_never_extracts_other_paths(self):
        assets = sample_assets()
        data = zip_bytes([(f"deepseek_v4_tokenizer/{name}", content) for name, content in assets.items()] + [("../../run.py", "raise AssertionError('must not run')"), ("demo.py", "raise AssertionError('must not run')")])
        installer.install_assets(installer.read_archive(data), self.destination)
        self.assertEqual({path.name for path in self.root.rglob("*") if path.is_file()}, set(installer.FILENAMES))
        self.assertEqual((self.destination / "tokenizer.json").read_bytes(), assets["tokenizer.json"])

    def test_duplicate_or_unsafe_json_archive_paths_are_rejected(self):
        assets = sample_assets()
        base = [(f"folder/{name}", content) for name, content in assets.items()]
        for extra in (("other/tokenizer.json", assets["tokenizer.json"]), ("../tokenizer.json", assets["tokenizer.json"]), ("/tokenizer.json", assets["tokenizer.json"])):
            with self.subTest(extra=extra[0]):
                with self.assertRaises(installer.InstallError):
                    installer.read_archive(zip_bytes(base + [extra]))
        self.assertFalse(self.destination.exists())

    def test_archive_missing_one_json_is_rejected(self):
        with self.assertRaises(installer.InstallError):
            installer.read_archive(zip_bytes([("tokenizer.json", sample_assets()["tokenizer.json"])]))

    def test_source_file_and_archive_size_limits_are_enforced(self):
        source = self.root / "source"
        source.mkdir()
        for name, data in sample_assets().items():
            (source / name).write_bytes(data)
        with patch.object(installer, "MAX_FILE_BYTES", 8):
            with self.assertRaises(installer.InstallError):
                installer.read_directory(source)
            with self.assertRaises(installer.InstallError):
                installer.read_archive(zip_bytes(sample_assets().items()))
        with patch.object(installer, "MAX_ARCHIVE_BYTES", 8):
            with self.assertRaises(installer.InstallError):
                installer.read_archive(b"x" * 9)

    def test_local_zip_can_be_used_without_a_network_call(self):
        source = self.root / "official.zip"
        assets = sample_assets()
        source.write_bytes(zip_bytes(assets.items()))
        self.assertEqual(installer.read_zip_file(source), assets)
        installer.urlopen.assert_not_called()

    def test_explicit_download_uses_only_fixed_official_url_and_a_timeout(self):
        assets = sample_assets()
        with patch.object(installer, "urlopen", return_value=BytesIO(zip_bytes(assets.items()))) as download:
            result = installer.download_official()
        self.assertEqual(result, assets)
        download.assert_called_once_with(installer.OFFICIAL_ARCHIVE_URL, timeout=installer.DOWNLOAD_TIMEOUT_SECONDS)

    def test_explicit_download_is_bounded_in_memory(self):
        with patch.object(installer, "MAX_ARCHIVE_BYTES", 8), patch.object(installer, "urlopen", return_value=BytesIO(b"x" * 9)):
            with self.assertRaises(installer.InstallError):
                installer.download_official()

    def test_cli_needs_an_explicit_source_and_does_not_download_by_default(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                installer.main([])
        self.assertEqual(raised.exception.code, 2)
        installer.urlopen.assert_not_called()

    def test_cli_local_failure_returns_nonzero_without_a_traceback(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = installer.main(["--source-dir", str(self.root / "missing")])
        self.assertEqual(status, 1)
        self.assertIn("installation failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        installer.urlopen.assert_not_called()

    def test_cli_installs_local_source_and_preserves_force_choice(self):
        source = self.root / "source"
        source.mkdir()
        for name, data in sample_assets().items():
            (source / name).write_bytes(data)
        with patch.object(installer, "install_assets", return_value=[self.destination / name for name in installer.FILENAMES]) as install, redirect_stdout(StringIO()):
            status = installer.main(["--source-dir", str(source), "--force"])
        self.assertEqual(status, 0)
        self.assertTrue(install.call_args.kwargs["force"])
        self.assertEqual(install.call_args.args[0], sample_assets())
        installer.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
