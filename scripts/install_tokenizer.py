#!/usr/bin/env python3
"""Optionally install only two DeepSeek tokenizer JSON files; never runs upstream code."""

import argparse
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import sys
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile


OFFICIAL_ARCHIVE_URL = "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"
DESTINATION = Path(__file__).resolve().parent.parent / "assets" / "deepseek_v4_tokenizer"
FILENAMES = ("tokenizer.json", "tokenizer_config.json")
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30


class InstallError(ValueError):
    """An optional source cannot be installed without changing existing assets."""


def _read_limited(stream, limit, label):
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise InstallError("%s exceeds the supported size limit" % label)
    return data


def read_directory(source_dir):
    """Read exactly the two expected files, without importing any source code."""
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        raise InstallError("Source directory does not exist")
    result = {}
    for name in FILENAMES:
        with (source_dir / name).open("rb") as stream:
            result[name] = _read_limited(stream, MAX_FILE_BYTES, name)
    return result


def read_archive(data):
    """Select JSON entries by basename; no archive path is extracted to disk."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise InstallError("Archive exceeds the supported size limit")
    result = {}
    with ZipFile(BytesIO(data)) as archive:
        for info in archive.infolist():
            member = PurePosixPath(info.filename.replace("\\", "/"))
            if info.is_dir() or member.name not in FILENAMES:
                continue
            if member.is_absolute() or ".." in member.parts:
                raise InstallError("Tokenizer archive contains an unsafe JSON entry path")
            if member.name in result:
                raise InstallError("Tokenizer archive contains duplicate %s entries" % member.name)
            if info.file_size > MAX_FILE_BYTES:
                raise InstallError("%s exceeds the supported size limit" % member.name)
            with archive.open(info) as stream:
                result[member.name] = _read_limited(stream, MAX_FILE_BYTES, member.name)
    missing = set(FILENAMES) - set(result)
    if missing:
        raise InstallError("Tokenizer archive is missing: %s" % ", ".join(sorted(missing)))
    return result


def read_zip_file(source_zip):
    with Path(source_zip).open("rb") as stream:
        return read_archive(_read_limited(stream, MAX_ARCHIVE_BYTES, "Archive"))


def download_official():
    """Download only after an explicit CLI choice; there is no retry or execution."""
    with urlopen(OFFICIAL_ARCHIVE_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        return read_archive(_read_limited(response, MAX_ARCHIVE_BYTES, "Official archive"))


def validate_assets(assets):
    """Check basic JSON contracts without requiring native optional dependencies."""
    if set(assets) != set(FILENAMES):
        raise InstallError("Exactly tokenizer.json and tokenizer_config.json are required")
    decoded = {}
    for name in FILENAMES:
        data = assets[name]
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise InstallError("%s must be a supported-size byte sequence" % name)
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise InstallError("%s is not a valid UTF-8 JSON document" % name) from exc
        if not isinstance(value, dict):
            raise InstallError("%s must contain a JSON object" % name)
        decoded[name] = value
    if not isinstance(decoded["tokenizer.json"].get("model"), dict):
        raise InstallError("tokenizer.json has no tokenizer model object")
    config = decoded["tokenizer_config.json"]
    if not isinstance(config.get("chat_template"), str) or not config["chat_template"].strip():
        raise InstallError("tokenizer_config.json has no chat_template string")
    bos = config.get("bos_token", "")
    if not isinstance(bos, (str, dict)):
        raise InstallError("tokenizer_config.json has an unsupported bos_token")
    return assets


def install_assets(assets, destination=DESTINATION, *, force=False):
    """Validate both files before writing either; existing files need --force."""
    validate_assets(assets)
    destination = Path(destination)
    targets = [destination / name for name in FILENAMES]
    if any(target.is_symlink() for target in targets):
        raise InstallError("Refusing to replace a symbolic link in the asset directory")
    if not force and any(target.exists() for target in targets):
        raise InstallError("Tokenizer assets already exist; use --force to replace them")
    if any(target.exists() and not target.is_file() for target in targets):
        raise InstallError("A tokenizer destination is not a regular file")
    destination.mkdir(parents=True, exist_ok=True)
    for target in targets:
        with target.open("wb" if force else "xb") as stream:
            stream.write(assets[target.name])
    return targets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-dir", type=Path, help="Local directory containing the two JSON files")
    source.add_argument("--source-zip", type=Path, help="Locally downloaded official tokenizer ZIP")
    source.add_argument("--download-official", action="store_true", help="Explicitly download the fixed official DeepSeek ZIP")
    parser.add_argument("--force", action="store_true", help="Replace existing local JSON assets")
    args = parser.parse_args(argv)
    try:
        if args.source_dir is not None:
            assets = read_directory(args.source_dir)
        elif args.source_zip is not None:
            assets = read_zip_file(args.source_zip)
        else:
            assets = download_official()
        targets = install_assets(assets, force=args.force)
    except (InstallError, OSError, BadZipFile, RuntimeError) as exc:
        print("Tokenizer installation failed: %s" % exc, file=sys.stderr)
        return 1
    print("Installed only: %s" % ", ".join(str(target) for target in targets))
    print("Optional runtime dependencies: python -m pip install -r requirements-tokenizer.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
