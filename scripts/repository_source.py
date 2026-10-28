"""Read-only repository material source with lazy, recorded file snapshots.

The external reviewer gets only the named functions, never a shell. Git reads
use fixed argv lists. A capture is a per-file read record, not an atomic or
content-fingerprinted snapshot of an entire repository.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess

try:
    from .atomic_io import atomic_write_json
    from .materials import MaterialStore
except ImportError:
    from atomic_io import atomic_write_json
    from materials import MaterialStore


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_INITIAL_BYTES = 64 * 1024 * 1024
MAX_SEARCH_BYTES = 8 * 1024 * 1024
MAX_SEARCH_FILES = 100
_SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', '__pycache__',
              '.pytest_cache', '.mypy_cache', 'dist', 'build', '.next', '.cache'}
_PRIVATE_NAMES = {'credentials.json', 'auth.json', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519',
                  '.netrc', '.npmrc', '.pypirc', 'api-key.txt', 'api_key.txt', 'deepseek-api-key.txt',
                  'secrets.json', 'secrets.yaml', 'secrets.yml', 'credentials.yaml', 'credentials.yml',
                  '.git-credentials', '.htpasswd', 'report_diagnostics.json', 'adjudication.json',
                  'private-decisions.json', 'local_io_errors.jsonl'}
_PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.keystore'}
_PRIVATE_ROOT_DIRS = {'logs', 'artifacts', 'outputs', 'reviews', '.review', '.third-party-review'}
# Exact known sidecars only. Ordinary project JSON and .tmp files stay readable.
_ATOMIC_RUNTIME_RECORDS = {
    'request.json', 'result.json', 'workflow.json', 'progress.json', 'token_budget.json',
    'materials.json', 'response_metadata.json', 'report_diagnostics.json',
    'adjudication.json', 'private-decisions.json', 'local_io_errors.jsonl',
}


def _runtime_temporary_name(name):
    if not name.endswith('.tmp'):
        return False
    stem = name[:-4]
    if stem in _ATOMIC_RUNTIME_RECORDS or (stem.startswith('adjudication-input') and stem.endswith('.json')):
        return True  # Legacy fixed-name temporary.
    base, separator, token = stem.rpartition('.')
    return bool(separator and token and (base in _ATOMIC_RUNTIME_RECORDS
                or (base.startswith('adjudication-input') and base.endswith('.json'))))


def _private_name(name):
    name = name.casefold()
    environment = (name == '.env' or name.startswith('.env.') or name.endswith('.env'))
    example = name.endswith(('.example', '.sample', '.template'))
    return (name in _PRIVATE_NAMES or _runtime_temporary_name(name) or name.startswith('credentials.')
            or (name.startswith('adjudication-input') and name.endswith('.json'))
            or (environment and not example) or Path(name).suffix in _PRIVATE_SUFFIXES)


class RepositoryError(ValueError):
    """A local source error whose message contains no raw Git diagnostics."""


def _write_json(path, value):
    atomic_write_json(path, value)


def _run_git(root, args, *, check=True):
    # These are per-process reads of an explicitly selected repository. Do
    # not inherit caller Git directory/index/object/config/trace overrides: a
    # GIT_DIR or GIT_INDEX_FILE must not redirect this source to another repo.
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('GIT_')}
    environment.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
                       GIT_LITERAL_PATHSPECS='0', GIT_GLOB_PATHSPECS='0',
                       GIT_NOGLOB_PATHSPECS='0', GIT_ICASE_PATHSPECS='0')
    # check-ignore takes literal path names itself and rejects Git's pathspec
    # magic, including a globally enabled literal-pathspecs mode. Every command
    # that accepts actual pathspecs instead gets literal mode explicitly.
    literal = [] if args and args[0] == 'check-ignore' else ['--literal-pathspecs']
    try:
        result = subprocess.run(
            ['git'] + literal + ['--no-pager', '-c', 'core.fsmonitor=false', '-C', str(root)] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            timeout=30, shell=False, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        raise RepositoryError('The read-only Git command could not complete') from None
    if check and result.returncode:
        raise RepositoryError('The selected repository or Git revision is unavailable')
    return result


def _strings(data):
    return [part.decode('utf-8', errors='replace') for part in data.split(b'\0') if part]


class RepositorySource(MaterialStore):
    """Repository exploration plus the existing packet material interface."""
    exploration_enabled = True

    def __init__(self, repo_root, *, workspace_roots=(), target='worktree', base=None,
                 task='', requirements=(), test_evidence=None, include_files=(), execution_context=(), output_dir=None,
                 max_chars=24000, max_lines=250, redactor=None, excluded_paths=()):
        self._redactor = redactor or (lambda value: value)
        requested = Path(repo_root).expanduser().resolve()
        if not requested.is_dir():
            raise RepositoryError('repo_root must be an existing Git working-tree directory')
        result = _run_git(requested, ['rev-parse', '--show-toplevel'])
        self.repo_root = Path(result.stdout.decode('utf-8').strip()).resolve()
        if target not in {'worktree', 'staged', 'range'}:
            raise RepositoryError('target must be worktree, staged, or range')
        if target == 'range' and (not isinstance(base, str) or not base or base.startswith('-') or '\0' in base):
            raise RepositoryError('range requires a non-option base revision')
        self.target, self.base = target, base
        self._has_head = _run_git(self.repo_root, ['rev-parse', '--verify', 'HEAD'], check=False).returncode == 0
        if target == 'range' and not self._has_head:
            raise RepositoryError('range requires an existing HEAD commit')
        self._base_ref = None
        if target == 'range':
            self._base_ref = _run_git(self.repo_root, ['merge-base', base, 'HEAD']).stdout.decode('ascii').strip()
        elif target == 'worktree' and self._has_head:
            self._base_ref = 'HEAD'
        self.roots = {'repo': self.repo_root}
        for index, path in enumerate(workspace_roots or (), 1):
            root = Path(path).expanduser().resolve()
            if not root.is_dir():
                raise RepositoryError('Every workspace root must be an existing directory')
            self.roots['workspace-' + str(index)] = root
        self._included_paths = []
        self._execution_context = list(execution_context or ())
        for file in include_files or ():
            candidate = Path(file).expanduser()
            candidate = (candidate if candidate.is_absolute() else self.repo_root / candidate).resolve()
            try:
                relative = candidate.relative_to(self.repo_root).as_posix()
            except ValueError:
                raise RepositoryError('Supplemental files must be inside the primary repository') from None
            if not candidate.is_file():
                raise RepositoryError('Each supplemental path must be an existing file, not a directory')
            if relative not in self._included_paths:
                self._included_paths.append(relative)
        if self._included_paths:
            self.roots['supplement'] = self.repo_root
        self.output_dir = Path(output_dir).resolve() if output_dir is not None else None
        self._excluded_paths = [Path(p).expanduser().resolve() for p in excluded_paths if p is not None]
        if self.output_dir is not None:
            if not self.output_dir.is_dir():
                raise RepositoryError('output_dir must be the already-created review directory')
            self._excluded_paths.append(self.output_dir)
            (self.output_dir / 'sources').mkdir(exist_ok=True)
        self._captures, self._cache, self._candidate_cache = [], {}, {}
        self._workspace_git = {}
        self._skipped, self._changes = [], []
        self._deleted_paths = set()
        self._skip_keys = set()
        self._manifest_batch_depth = 1
        self._manifest_dirty = False
        self._ready = False
        try:
            packet = self._collect(task, requirements, test_evidence)
            super().__init__(packet, max_chars=max_chars, max_lines=max_lines)
            self._ready = True
            for capture in self._captures:
                if not capture.get('initial_omitted'):
                    self._register(capture)
        finally:
            # Retain the index of any already-written text even when a later
            # collection command fails and the caller records a source error.
            self._manifest_batch_depth = 0
            self._persist()

    @property
    def packet(self):
        return self._packet

    def _redact(self, value):
        if isinstance(value, str):
            return self._redactor(value)
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        return value

    def _skip(self, root, path, reason):
        entry = self._redact({'root': root, 'path': path, 'reason': reason})
        key = tuple(entry[field] for field in ('root', 'path', 'reason'))
        if key not in self._skip_keys:
            self._skip_keys.add(key)
            self._skipped.append(entry)
            self._manifest_dirty = True

    @staticmethod
    def _relative(path, *, allow_empty=False):
        if not isinstance(path, str) or '\0' in path:
            raise RepositoryError('path must be a relative string')
        if not path and allow_empty:
            return ''
        normalized = path.replace('\\', '/')
        parsed = PurePosixPath(normalized)
        if not normalized or parsed.is_absolute() or PureWindowsPath(path).drive or '..' in parsed.parts:
            raise RepositoryError('path must remain inside the selected root')
        if normalized.startswith('//') or ':' in normalized:
            raise RepositoryError('absolute and drive-qualified paths are not allowed')
        return parsed.as_posix() if parsed.as_posix() != '.' else ''

    def _check_path(self, root, path, *, allow_empty=False):
        if not isinstance(root, str) or root not in self.roots:
            raise RepositoryError('Unknown root alias')
        relative = self._relative(path, allow_empty=allow_empty)
        if root == 'supplement' and relative not in self._included_paths:
            prefix = relative.rstrip('/') + '/' if relative else ''
            if not allow_empty or not any(item.startswith(prefix) for item in self._included_paths):
                raise RepositoryError('Only explicitly included supplemental files are available')
        parts = PurePosixPath(relative).parts
        if any(part.casefold() in _SKIP_DIRS for part in parts):
            raise RepositoryError('This path is excluded from source exploration')
        if root != 'supplement' and parts and parts[0].casefold() in _PRIVATE_ROOT_DIRS:
            raise RepositoryError('Local review/runtime output directories are excluded')
        name = parts[-1].casefold() if parts else ''
        if (_private_name(name)
                or any(part.casefold() in {'.ssh', '.aws', '.bailian'} for part in parts)):
            raise RepositoryError('Credential and private-key paths are excluded')
        if self._redact(relative) != relative:
            raise RepositoryError('A path containing a configured credential is excluded')
        lexical = self.roots[root] / Path(relative)
        # Git index/commit reads do not follow working-tree symlinks. A dirty
        # worktree must not change which indexed/committed source is selected.
        git_view = root == 'repo' and (self.target in {'staged', 'range'} or relative in self._deleted_paths)
        resolved = lexical if git_view else lexical.resolve()
        try:
            resolved.relative_to(self.roots[root])
        except ValueError:
            raise RepositoryError('A symlink or path escapes the selected root') from None
        # Also check the resolved name: an innocuous symlink must not expose a
        # private file whose real name is otherwise excluded.
        resolved_relative = resolved.relative_to(self.roots[root]).as_posix()
        resolved_parts = PurePosixPath(resolved_relative).parts
        if root != 'supplement' and resolved_parts and resolved_parts[0].casefold() in _PRIVATE_ROOT_DIRS:
            raise RepositoryError('The resolved runtime output directory is excluded')
        if root == 'supplement' and relative in self._included_paths and resolved_relative not in self._included_paths:
            raise RepositoryError('Supplemental path no longer refers to an included file')
        resolved_name = resolved_parts[-1].casefold() if resolved_parts else ''
        if (any(part.casefold() in _SKIP_DIRS for part in resolved_parts)
                or _private_name(resolved_name)
                or any(part.casefold() in {'.ssh', '.aws', '.bailian'} for part in resolved_parts)):
            raise RepositoryError('The resolved source path is excluded')
        for excluded in self._excluded_paths:
            if resolved == excluded or excluded in resolved.parents:
                raise RepositoryError('This local runtime path is excluded')
        return relative, resolved

    def _view(self, root):
        return self.target if root == 'repo' else 'worktree'

    def _selected_view(self, root, relative):
        # A file known to be deleted by this review target is still relevant:
        # read its selected baseline without adding an arbitrary-revision tool.
        if root == 'repo' and relative in self._deleted_paths:
            return 'base'
        return self._view(root)

    def _is_git_worktree(self, root):
        if root == 'repo':
            return True
        if root not in self._workspace_git:
            result = _run_git(self.roots[root], ['rev-parse', '--is-inside-work-tree'], check=False)
            self._workspace_git[root] = result.returncode == 0 and result.stdout.strip() == b'true'
        return self._workspace_git[root]

    def _check_git_ignore(self, root, relative, disk_path):
        if root == 'supplement':
            return  # Explicit files only; _check_path still applies credential/output exclusions.
        if not self._is_git_worktree(root):
            return
        # An allowed-looking symlink name cannot expose an ignored target in
        # the same root. This is only for disk reads, never index/commit blobs.
        resolved_relative = disk_path.relative_to(self.roots[root]).as_posix()
        for candidate in dict.fromkeys([relative, resolved_relative]):
            ignored = _run_git(self.roots[root], ['check-ignore', '--quiet', '--', candidate], check=False)
            if ignored.returncode == 0:
                raise RepositoryError('Git-ignored working-tree files are excluded')
            if ignored.returncode != 1:
                raise RepositoryError('The source ignore policy could not be checked')

    def _load_bytes(self, root, relative, view=None):
        relative, disk_path = self._check_path(root, relative)
        view = view or self._selected_view(root, relative)
        if root == 'repo' and view in {'staged', 'range', 'base'}:
            if view == 'staged':
                spec = ':' + relative
            elif view == 'base':
                if self.target == 'staged':
                    if not self._has_head:
                        raise RepositoryError('No baseline content exists')
                    spec = 'HEAD:' + relative
                else:
                    spec = str(self._base_ref) + ':' + relative
            else:
                spec = 'HEAD:' + relative
            size = _run_git(self.repo_root, ['cat-file', '-s', spec], check=False)
            if size.returncode:
                raise RepositoryError('File is absent from the selected Git view')
            if int(size.stdout.strip()) > MAX_FILE_BYTES:
                raise RepositoryError('File exceeds the documented 8 MiB source limit')
            data = _run_git(self.repo_root, ['show', '--no-ext-diff', '--no-textconv', spec]).stdout
            return data, None
        self._check_git_ignore(root, relative, disk_path)
        if not disk_path.is_file():
            raise RepositoryError('File is not present in this working tree')
        stat = disk_path.stat()
        if stat.st_size > MAX_FILE_BYTES:
            raise RepositoryError('File exceeds the documented 8 MiB source limit')
        with disk_path.open('rb') as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise RepositoryError('File exceeds the documented 8 MiB source limit')
        return data, stat.st_mtime_ns

    def _capture(self, root, relative, view=None):
        relative, _ = self._check_path(root, relative)
        view = view or self._selected_view(root, relative)
        key = (root, relative, view)
        if key in self._cache:
            capture = self._cache[key]
            if self._ready and not capture.get('registered'):
                self._register(capture)
                self._persist()
            return capture
        data, mtime = self._load_bytes(root, relative, view)
        if b'\0' in data:
            raise RepositoryError('Binary content is not exposed by text tools')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            raise RepositoryError('File is not UTF-8 text') from None
        text = self._redact(text)
        source_id = 'source-%04d' % (len(self._captures) + 1)
        public_path = root + '/' + relative + ('@base' if view == 'base' else '')
        capture = {'source_id': source_id, 'root': root, 'relative_path': relative,
                   'path': public_path, 'view': view, 'captured_utc': datetime.now(timezone.utc).isoformat(),
                   'bytes_read': len(data), 'chars': len(text), 'lines': len(text.splitlines()),
                   'mtime_ns': mtime, 'content': text, 'material_path': public_path,
                   'snapshot_file': 'sources/' + source_id + '.txt'}
        self._captures.append(capture)
        self._cache[key] = capture
        if self.output_dir is not None:
            (self.output_dir / capture['snapshot_file']).write_bytes(text.encode('utf-8'))
        if self._ready:
            self._register(capture)
        self._persist()
        return capture

    def _register(self, capture):
        alias = self._add(capture['path'], capture['content'], 'file', source_path=capture['path'],
                          note='Recorded ' + capture['source_id'] + '; first-read ' + capture['view'] + ' content.')
        capture['material_path'] = alias
        self._find(alias)['source_id'] = capture['source_id']
        capture['registered'] = True

    def _persist(self):
        # Source text is retained immediately. The growing metadata manifest is
        # written once after initial collection or one complete tool request.
        self._manifest_dirty = True
        if self._manifest_batch_depth:
            return
        if self.output_dir is not None:
            _write_json(self.output_dir / 'sources/index.json', self.repository_manifest())
        self._manifest_dirty = False

    def _change_args(self):
        if self.target == 'staged':
            return ['--cached']
        if self.target == 'range':
            return [self._base_ref, 'HEAD']
        return ['HEAD'] if self._has_head else ['--cached']

    def _collect(self, task, requirements, test_evidence):
        status = _run_git(self.repo_root, ['diff', '--no-ext-diff', '--no-textconv', '--name-status', '-z', '-M'] + self._change_args() + ['--'])
        values, cursor = _strings(status.stdout), 0
        changes = []
        while cursor < len(values):
            code = values[cursor]; cursor += 1
            if cursor >= len(values):
                raise RepositoryError('Git returned an incomplete change list')
            old = values[cursor]; cursor += 1
            new = old
            if code.startswith(('R', 'C')):
                if cursor >= len(values):
                    raise RepositoryError('Git returned an incomplete rename')
                new = values[cursor]; cursor += 1
            changes.append({'status': code, 'path': new, 'old_path': old if old != new else None})
        if self.target == 'worktree':
            known = {item['path'] for item in changes}
            for path in _strings(_run_git(self.repo_root, ['ls-files', '--others', '--exclude-standard', '-z']).stdout):
                if path not in known:
                    changes.append({'status': 'untracked', 'path': path, 'old_path': None})
        # An unborn worktree has no HEAD to compare against. Every tracked
        # working file is new even if it is not yet staged.
        if self.target == 'worktree' and not self._has_head:
            known = {item['path'] for item in changes}
            for path in _strings(_run_git(self.repo_root, ['ls-files', '-z']).stdout):
                if path not in known:
                    changes.append({'status': 'A', 'path': path, 'old_path': None})
        self._deleted_paths = {item['path'] for item in changes if item['status'].startswith('D')}
        files, diffs, total = [], [], 0
        diff_groups = []
        for change in changes:
            self._changes.append(self._redact(change))
            path = change['path']
            try:
                self._check_path('repo', path)
                if change['old_path']:
                    self._check_path('repo', change['old_path'])
                # Deleted sources are retained from the selected baseline,
                # not loaded from an unrelated current worktree file.
                if total >= MAX_INITIAL_BYTES:
                    self._skip('repo', path, 'Initial material limit reached; source remains available through read_file')
                    continue
                view = 'base' if change['status'].startswith('D') else None
                capture = self._capture('repo', path, view)
                if total + len(capture['content'].encode('utf-8')) > MAX_INITIAL_BYTES:
                    capture['initial_omitted'] = True
                    self._skip('repo', path, 'Initial material limit reached; source remains available through read_file')
                    continue
                total += len(capture['content'].encode('utf-8'))
                files.append({'path': capture['path'], 'content': capture['content'], 'source_id': capture['source_id']})
                if change['status'] == 'untracked' or (self.target == 'worktree' and not self._has_head):
                    diffs.append('New working-tree file: ' + path + '\n')
                else:
                    paths = [path] + ([change['old_path']] if change['old_path'] else [])
                    diff_groups.append(paths)
            except (OSError, RepositoryError, ValueError) as exc:
                self._skip('repo', path, str(exc) if isinstance(exc, RepositoryError) else 'Source could not be read')
        # Keep rename source/destination pairs together and bound argv length
        # for Windows. The global literal-pathspec setting still applies.
        batches, batch, argument_chars = [], [], 0
        for group in diff_groups:
            cost = sum(len(path) + 3 for path in group)
            if cost > 12000:
                self._skip('repo', group[0], 'Diff path arguments exceed the 12K batching limit; captured file content is retained')
                continue
            if batch and argument_chars + cost > 12000:
                batches.append(batch)
                batch, argument_chars = [], 0
            batch.extend(group)
            argument_chars += cost
        if batch:
            batches.append(batch)
        for paths in batches:
            data = _run_git(self.repo_root, ['diff', '--no-ext-diff', '--no-textconv', '--function-context', '-M']
                            + self._change_args() + ['--'] + list(dict.fromkeys(paths))).stdout
            diffs.append(self._redact(data.decode('utf-8', errors='replace')))
        for relative in self._included_paths:
            capture = self._capture('supplement', relative)
            if total + capture['bytes_read'] > MAX_INITIAL_BYTES:
                capture['initial_omitted'] = True
                self._skip('supplement', relative, 'Initial material limit reached; explicit file remains available through read_file')
                continue
            total += capture['bytes_read']
            files.append({'path': capture['path'], 'content': capture['content'], 'source_id': capture['source_id']})
        packet = {'task': self._redact(task or ('代码审阅：' + self.repo_root.name + ' (' + self.target + ')')),
                  'requirements': self._redact(list(requirements or ())), 'diff': ''.join(diffs), 'files': files,
                  'scope': self.exploration_context()}
        if self._execution_context:
            packet['scope']['execution_context'] = self._redact(self._execution_context)
        if test_evidence is not None:
            if isinstance(test_evidence, Path):
                if test_evidence.stat().st_size > MAX_FILE_BYTES:
                    raise RepositoryError('Test evidence exceeds the source byte limit')
                packet['test_results'] = self._redact(test_evidence.read_text(encoding='utf-8'))
            else:
                packet['test_results'] = self._redact(str(test_evidence))
        return packet

    def exploration_context(self):
        context = {'mode': 'repository', 'target': self.target,
                'roots': [{'alias': alias, 'view': self._view(alias)} for alias in self.roots],
                'instruction': 'Use list_files/read_file/search_code to investigate callers, contracts and tests within these roots. Initial inline material is not the whole repository. Known deleted repo files remain discoverable in list_files entries and read_file uses their selected baseline (view=base). First-read file contents are cached for this review; captures are not an atomic repository snapshot.',
                'initial_changes': self._changes[:100], 'initial_changes_total': len(self._changes),
                'omitted_initial_sources': self._skipped[:50], 'omitted_initial_sources_total': len(self._skipped)}
        if self._included_paths:
            context.update(supplemental_files=self._redact(self._included_paths),
                           supplement_note='supplement exposes only explicitly supplied current-disk files, independently of the selected repo Git view.')
        return context

    def repository_manifest(self):
        return self._redact({'mode': 'repository', 'target': self.target, 'base': self.base,
                            'roots': [{'alias': alias, 'path': str(path), 'view': self._view(alias)} for alias, path in self.roots.items()],
                            'changes': self._changes, 'skipped': self._skipped,
                            'sources': [{k: v for k, v in item.items() if k != 'content'} for item in self._captures],
                            'consistency': 'Per-file first-read captures, not an atomic repository snapshot.',
                            'policy': {'max_file_bytes': MAX_FILE_BYTES, 'max_initial_bytes': MAX_INITIAL_BYTES,
                                       'text_encoding': 'UTF-8', 'ignored_untracked': 'Excluded by git --exclude-standard',
                                       'search': 'First match per file; bounded scan with a file cursor'}})

    def diagnostic_sources(self):
        return [{'path': item['path'], 'aliases': [item['relative_path'], item['material_path']],
                 'source_id': item['source_id'], 'content': item['content']} for item in self._captures]

    def index(self):
        result = super().index()
        for item in result:
            capture = next((c for c in self._captures if c['material_path'] == item['path']), None)
            if capture:
                item['source_id'] = capture['source_id']
        return result

    def _candidate_paths(self, root):
        if root in self._candidate_cache:
            return self._candidate_cache[root]
        if not isinstance(root, str) or root not in self.roots:
            raise RepositoryError('Unknown root alias')
        if root == 'supplement':
            paths = self._included_paths[:]
        elif root == 'repo':
            if self.target == 'range':
                paths = _strings(_run_git(self.repo_root, ['ls-tree', '-r', '--name-only', '-z', 'HEAD']).stdout)
            else:
                paths = _strings(_run_git(self.repo_root, ['ls-files', '--cached', '-z']).stdout)
                if self.target == 'worktree':
                    paths += _strings(_run_git(self.repo_root, ['ls-files', '--others', '--exclude-standard', '-z']).stdout)
        elif self._is_git_worktree(root):
            paths = _strings(_run_git(self.roots[root], ['ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', '.']).stdout)
        else:
            paths = []
            for directory, dirs, files in os.walk(self.roots[root], followlinks=False):
                retained = []
                for name in sorted(dirs):
                    candidate = Path(directory) / name
                    if candidate.is_symlink():
                        continue
                    try:
                        self._check_path(root, candidate.relative_to(self.roots[root]).as_posix())
                    except (OSError, RepositoryError):
                        continue
                    retained.append(name)
                dirs[:] = retained
                paths.extend((Path(directory) / name).relative_to(self.roots[root]).as_posix() for name in sorted(files))
        if root == 'repo':
            paths += sorted(self._deleted_paths)
        allowed = []
        for path in sorted(set(paths)):
            try:
                relative, actual = self._check_path(root, path)
                if self._view(root) == 'worktree' and not actual.is_file() and not (root == 'repo' and relative in self._deleted_paths):
                    continue
                allowed.append(relative)
            except (OSError, RepositoryError):
                continue
        self._candidate_cache[root] = allowed
        return allowed

    def tools(self):
        tools = super().tools()
        root = {'type': 'string', 'enum': list(self.roots), 'default': 'repo'}
        path = {'type': 'string', 'description': 'Root-relative file or directory path; no absolute paths or parent traversal.'}
        for name, description, properties, required in [
            ('list_files', 'List source names with a file cursor; entries mark known deleted repo files as deleted/base.',
             {'root': root, 'path': dict(path, default=''), 'offset': {'type': 'integer', 'minimum': 0, 'default': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 50}}, []),
            ('read_file', 'Read paginated text; known deleted repo files use the selected baseline. First reads are recorded and reused.',
             {'root': root, 'path': path, 'start_line': {'type': 'integer', 'minimum': 1, 'default': 1}, 'line_count': {'type': 'integer', 'minimum': 1, 'default': self.max_lines}, 'char_offset': {'type': 'integer', 'minimum': 0, 'default': 0}}, ['path']),
            ('search_code', 'Literal case-sensitive search, first matching excerpt per file. offset is a file cursor; read_file supplies all matching-file context. Each call scans at most 100 files/approximately 8 MiB.',
             {'root': root, 'path': dict(path, default=''), 'query': {'type': 'string', 'minLength': 1, 'maxLength': 160}, 'offset': {'type': 'integer', 'minimum': 0, 'default': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'default': 20}}, ['query']),
        ]:
            tools.append({'type': 'function', 'function': {'name': name, 'description': description,
                          'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}})
        return tools

    def execute(self, name, arguments):
        self._manifest_batch_depth += 1
        try:
            return self._execute(name, arguments)
        finally:
            self._manifest_batch_depth -= 1
            if not self._manifest_batch_depth and self._manifest_dirty:
                self._persist()

    def _execute(self, name, arguments):
        if name not in {'list_files', 'read_file', 'search_code'}:
            return super().execute(name, arguments)
        args = self._arguments_object(arguments)
        if args is None:
            return self._error('arguments must be a JSON object')
        permitted = {'root', 'path', 'start_line', 'line_count', 'char_offset'} if name == 'read_file' else {'root', 'path', 'offset', 'limit'} | ({'query'} if name == 'search_code' else set())
        if set(args) - permitted:
            return self._error('unknown argument')
        root, path = args.get('root', 'repo'), args.get('path', '')
        try:
            relative, _ = self._check_path(root, path, allow_empty=name != 'read_file')
            if name == 'read_file':
                capture = self._capture(root, relative)
                request = {k: args[k] for k in ('start_line', 'line_count', 'char_offset') if k in args}
                request['path'] = capture['material_path']
                result = super().execute('read_material', request)
                if 'error' not in result:
                    result.update(path=capture['path'], source_id=capture['source_id'], root=root, view=capture['view'])
                return result
            offset, limit = args.get('offset', 0), args.get('limit', 50 if name == 'list_files' else 20)
            if not self._valid_int(offset) or not self._valid_int(limit, 1):
                return self._error('offset and limit must be non-negative/positive integers')
            limit = min(limit, 50 if name == 'list_files' else 20)
            candidates = [p for p in self._candidate_paths(root) if not relative or p == relative or p.startswith(relative.rstrip('/') + '/')]
            if name == 'list_files':
                end = min(offset + limit, len(candidates))
                selected = candidates[offset:end]
                entries = [{'path': filename, 'view': self._selected_view(root, filename),
                            'status': 'deleted' if root == 'repo' and filename in self._deleted_paths else 'available'}
                           for filename in selected]
                return {'root': root, 'files': selected, 'entries': entries, 'offset': offset, 'total': len(candidates),
                        'has_more': end < len(candidates), 'next_offset': end if end < len(candidates) else None}
            query = args.get('query')
            if not isinstance(query, str) or not query or len(query) > 160:
                return self._error('query must contain 1 through 160 characters')
            matches, cursor, scanned, byte_count, skipped = [], offset, 0, 0, 0
            while cursor < len(candidates) and scanned < MAX_SEARCH_FILES and byte_count < MAX_SEARCH_BYTES and len(matches) < limit:
                filename = candidates[cursor]; cursor += 1; scanned += 1
                try:
                    capture = self._capture(root, filename)
                except (RepositoryError, OSError) as exc:
                    skipped += 1
                    self._skip(root, filename, str(exc) if isinstance(exc, RepositoryError) else 'Source could not be read')
                    continue
                text = capture['content']; byte_count += len(text.encode('utf-8'))
                position = text.find(query)
                if position >= 0:
                    line = text.count('\n', 0, position) + 1
                    line_start = text.rfind('\n', 0, position) + 1
                    matches.append({'path': capture['path'], 'root': root, 'source_id': capture['source_id'], 'view': capture['view'],
                                    'line': line, 'line_numbers': [line], 'char_offset': position - line_start,
                                    'snippet': text[max(0, position-72):position+len(query)+72]})
            self._persist()
            return {'root': root, 'matches': matches, 'offset': offset, 'next_offset': cursor if cursor < len(candidates) else None,
                    'has_more': cursor < len(candidates), 'scanned_files': scanned, 'skipped_files': skipped,
                    'match_policy': 'First matching excerpt per file; read_file for complete file context.'}
        except (RepositoryError, OSError, ValueError) as exc:
            self._skip(root if isinstance(root, str) else 'unknown', path if isinstance(path, str) else '',
                       str(exc) if isinstance(exc, RepositoryError) else 'Source could not be read')
            self._persist()
            return self._error(str(exc) if isinstance(exc, RepositoryError) else 'Source could not be read')
