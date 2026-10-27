"""Offline repository-source tests using isolated Git fixtures; no model calls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import repository_source as sources


class RepositorySourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'repo'
        self.root.mkdir()
        self.git('init', '-q')
        self.write('app.py', 'def calculate(value):\n    return value + 1\n')
        self.write('caller.py', 'from app import calculate\nresult = calculate(1)\n')
        self.write('.gitignore', 'ignored.txt\n')
        self.commit('baseline')
        self.out = Path(self.temp.name).resolve() / 'review'
        self.out.mkdir()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True).stdout

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):path.write_bytes(content)
        else:path.write_bytes(content.encode('utf-8'))
        return path

    def commit(self, message):
        self.git('add', '--all')
        self.git('-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath='+str(self.root/'no-hooks'), 'commit', '-qm', message)

    def source(self, **kwargs):
        return sources.RepositorySource(self.root, output_dir=self.out, **kwargs)

    def test_worktree_collects_staged_unstaged_and_new_but_not_ignored(self):
        self.write('app.py', 'value = "staged"\n')
        self.git('add', 'app.py')
        self.write('app.py', 'value = "working"\n')
        self.write('new.py', 'NEW_FILE = True\n')
        self.write('ignored.txt', 'not collected\n')
        source = self.source(task='Review ordinary changes')
        files = {f['path']:f['content'] for f in source.packet['files']}
        self.assertEqual(files['repo/app.py'], 'value = "working"\n')
        self.assertIn('repo/new.py', files)
        self.assertNotIn('repo/ignored.txt', files)
        self.assertIn('working', source.packet['diff'])
        self.assertTrue(source.exploration_enabled)
        self.assertIn('read_file', [t['function']['name'] for t in source.tools()])
        self.assertIn('read_material', [t['function']['name'] for t in source.tools()])
        self.assertEqual(source.exploration_context()['roots'][0]['alias'], 'repo')

    def test_staged_collect_and_exploration_use_index_not_working_tree(self):
        self.write('app.py', 'value = "indexed"\n'); self.git('add', 'app.py')
        self.write('app.py', 'value = "dirty"\n')
        self.write('caller.py', 'caller = "dirty"\n')
        self.write('new.py', 'not staged\n')
        source = self.source(target='staged')
        self.assertIn('indexed', source.packet['files'][0]['content'])
        read = source.execute('read_file', {'path':'caller.py'})
        self.assertIn('from app', read['text'])
        self.assertNotIn('dirty', read['text'])
        self.assertIn('error', source.execute('read_file', {'path':'new.py'}))
        self.assertNotIn('new.py', source.execute('list_files', {})['files'])

    def test_staged_git_content_does_not_follow_dirty_worktree_symlink(self):
        self.write('app.py', 'value = "indexed"\n'); self.git('add', 'app.py')
        outside = Path(self.temp.name)/'external.py'; outside.write_text('private outside')
        (self.root/'app.py').unlink()
        try:(self.root/'app.py').symlink_to(outside)
        except (OSError,NotImplementedError):self.skipTest('Symlink creation unavailable')
        source = self.source(target='staged')
        self.assertEqual(source.execute('read_file',{'path':'app.py'})['text'],'value = "indexed"\n')

    def test_range_uses_head_and_deletion_keeps_baseline(self):
        self.write('app.py', 'value = "committed"\n')
        (self.root/'caller.py').unlink()
        self.commit('change')
        self.write('app.py', 'value = "dirty"\n')
        source = self.source(target='range', base='HEAD~1')
        by_path = {f['path']:f['content'] for f in source.packet['files']}
        self.assertIn('committed', by_path['repo/app.py'])
        self.assertIn('from app', by_path['repo/caller.py@base'])
        self.assertIn('committed', source.execute('read_file', {'path':'app.py'})['text'])
        deleted=source.execute('read_file', {'path':'caller.py'})
        self.assertEqual(deleted['view'],'base')
        self.assertEqual(deleted['path'],'repo/caller.py@base')
        self.assertIn('from app',deleted['text'])
        self.assertTrue(any(c['status'].startswith('D') for c in source.repository_manifest()['changes']))

    def test_rename_and_deleted_worktree_are_recorded(self):
        self.git('mv', 'app.py', 'renamed.py')
        (self.root/'caller.py').unlink()
        source = self.source()
        changes = source.repository_manifest()['changes']
        rename = next(item for item in changes if item['status'].startswith('R'))
        self.assertEqual(rename['old_path'], 'app.py')
        self.assertEqual(rename['path'], 'renamed.py')
        self.assertIn('repo/caller.py@base', [f['path'] for f in source.packet['files']])

    def test_new_related_files_can_be_explored_and_first_read_is_cached(self):
        self.write('app.py', 'changed = True\n')
        source = self.source(max_chars=18, max_lines=1)
        self.assertNotIn('repo/caller.py', [f['path'] for f in source.packet['files']])
        page = source.execute('read_file', {'path':'caller.py'})
        self.assertEqual(page['source_id'], 'source-0002')
        self.assertTrue(page['has_more'])
        capture_path = self.out/'sources'/ (page['source_id']+'.txt')
        self.assertEqual(capture_path.read_text(encoding='utf-8'), 'from app import calculate\nresult = calculate(1)\n')
        self.write('caller.py', 'changed after review read\n')
        again = source.execute('read_file', {'path':'caller.py', 'start_line':page['next_start_line'], 'char_offset':page['next_char_offset']})
        self.assertEqual(again['source_id'],page['source_id'])
        self.assertNotIn('changed after',again['text'])
        self.assertEqual(len(source.diagnostic_sources()),2)
        self.assertIn('not an atomic', source.repository_manifest()['consistency'])

    def test_paths_credentials_binary_and_runtime_files_are_excluded(self):
        self.write('app.py', 'changed = True\n')
        self.write('.env', 'PASSWORD=private\n')
        self.write('.env.example', 'PASSWORD=placeholder\n')
        self.write('config.json', '{"feature": true}\n')
        self.write('binary.dat', b'\x00secret')
        hidden = self.write('private-runtime.txt','private runtime')
        source = self.source(excluded_paths=[hidden])
        for path in ['../outside.py', '/etc/passwd', 'C:\\outside.py', '.git/config', '.env', 'private-runtime.txt','binary.dat']:
            with self.subTest(path=path):self.assertIn('error',source.execute('read_file',{'path':path}))
        self.assertIn('feature',source.execute('read_file',{'path':'config.json'})['text'])
        self.assertIn('placeholder',source.execute('read_file',{'path':'.env.example'})['text'])
        self.assertIn('error',source.execute('read_file',{'root':{},'path':'app.py'}))
        reasons = source.repository_manifest()['skipped']
        self.assertTrue(any('Binary' in row['reason'] for row in reasons))

    def test_ignored_and_private_output_paths_cannot_be_guessed_but_source_logs_can(self):
        self.write('ignored.txt', 'private ignored data\n')
        self.write('artifacts/old-review/reasoning.txt', 'private old review\n')
        self.write('logs/calls.txt', 'private call log\n')
        self.write('src/logs/logger.py', 'def log(value): return value\n')
        source = self.source()
        for path in ['ignored.txt','artifacts/old-review/reasoning.txt','logs/calls.txt']:
            with self.subTest(path=path):self.assertIn('error',source.execute('read_file',{'path':path}))
        self.assertIn('def log', source.execute('read_file',{'path':'src/logs/logger.py'})['text'])
        listed = source.execute('list_files',{})['files']
        self.assertNotIn('artifacts/old-review/reasoning.txt',listed)

    def test_symlink_escape_is_rejected_when_available(self):
        outside = Path(self.temp.name)/'outside.txt'; outside.write_text('outside',encoding='utf-8')
        try:(self.root/'escape.txt').symlink_to(outside)
        except (OSError,NotImplementedError):self.skipTest('Symlink creation unavailable')
        source = self.source()
        self.assertIn('error',source.execute('read_file',{'path':'escape.txt'}))

    def test_workspace_listing_search_paging_and_source_association(self):
        workspace = Path(self.temp.name)/'shared'; workspace.mkdir()
        for i in range(3):(workspace/('file%d.py'%i)).write_text('needle\nsecond needle\n',encoding='utf-8')
        (workspace/'.env').write_text('needle private',encoding='utf-8')
        source = self.source(workspace_roots=[workspace])
        listed = source.execute('list_files',{'root':'workspace-1','limit':2})
        self.assertEqual(len(listed['files']),2)
        self.assertTrue(listed['has_more'])
        self.assertNotIn('.env',listed['files'])
        search = source.execute('search_code',{'root':'workspace-1','query':'needle','limit':2})
        self.assertEqual(len(search['matches']),2)
        self.assertTrue(search['has_more'])
        tail = source.execute('search_code',{'root':'workspace-1','query':'needle','offset':search['next_offset']})
        self.assertEqual(len(tail['matches']),1)
        first = search['matches'][0]
        self.assertIn(first['source_id'], [x['source_id'] for x in source.diagnostic_sources()])
        self.assertEqual(first['line_numbers'],[1])

    def test_known_key_is_redacted_in_packet_capture_index_and_tools(self):
        key='offline-secret-never-live'
        self.write('app.py','token = "'+key+'"\n')
        self.write('new.py','also = "'+key+'"\n')
        source = self.source(task=key,redactor=lambda value:value.replace(key,'[REDACTED]'))
        self.assertNotIn(key,json.dumps(source.packet))
        self.assertNotIn(key,json.dumps(source.repository_manifest()))
        read=source.execute('read_file',{'path':'new.py'})
        self.assertIn('[REDACTED]',read['text'])
        for path in (self.out/'sources').iterdir():self.assertNotIn(key,path.read_text(encoding='utf-8'))

    def test_large_initial_source_is_reported_not_silently_missing(self):
        self.write('big.py','x'*100)
        with patch.object(sources,'MAX_FILE_BYTES',50):
            source=self.source()
            self.assertTrue(any(row['path']=='big.py' and 'limit' in row['reason'] for row in source.repository_manifest()['skipped']))
            self.assertIn('error',source.execute('read_file',{'path':'big.py'}))

    def test_initial_resource_omission_is_available_on_demand_without_inline_leak(self):
        self.write('new.py','TOO_LARGE_FOR_SMALL_INITIAL_LIMIT = True\n')
        with patch.object(sources,'MAX_INITIAL_BYTES',8):source=self.source()
        self.assertEqual(source.packet['files'],[])
        self.assertNotIn('repo/new.py',[item['path'] for item in source.index()])
        self.assertEqual(source.exploration_context()['omitted_initial_sources_total'],1)
        page=source.execute('read_file',{'path':'new.py'})
        self.assertIn('TOO_LARGE_FOR_SMALL_INITIAL_LIMIT',page['text'])
        self.assertIn('repo/new.py',[item['path'] for item in source.index()])

    def _assert_literal_diff_name_cannot_include_excluded_source(self, literal_name, other_name):
        self.write(literal_name, 'literal = "baseline"\n')
        private=self.write(other_name, 'private = "baseline"\n')
        self.commit('literal path fixtures')
        self.write(literal_name, 'literal = "changed"\n')
        self.write(other_name, 'EXCLUDED_PRIVATE_SENTINEL = True\n')
        source=self.source(excluded_paths=[private])
        self.assertEqual([f['path'] for f in source.packet['files']],['repo/'+literal_name])
        self.assertIn('literal = "changed"',source.packet['diff'])
        self.assertNotIn('EXCLUDED_PRIVATE_SENTINEL',source.packet['diff'])
        for capture in source.diagnostic_sources():self.assertNotIn('EXCLUDED_PRIVATE_SENTINEL',capture['content'])

    def test_bracket_filename_is_literal_in_all_git_pathspecs(self):
        self._assert_literal_diff_name_cannot_include_excluded_source('a[1].py','a1.py')

    @unittest.skipIf(os.name=='nt','Windows filenames cannot contain a literal asterisk')
    def test_asterisk_filename_is_literal_in_all_git_pathspecs(self):
        self._assert_literal_diff_name_cannot_include_excluded_source('literal*.py','literal-other.py')

    def test_workspace_walk_prunes_private_and_explicitly_excluded_directories(self):
        workspace=Path(self.temp.name)/'workspace';workspace.mkdir()
        (workspace/'artifacts').mkdir();(workspace/'logs').mkdir();(workspace/'custom-private').mkdir()
        (workspace/'src/logs').mkdir(parents=True)
        (workspace/'src/logs/logger.py').write_text('source = True\n',encoding='utf-8')
        source=self.source(workspace_roots=[workspace],excluded_paths=[workspace/'custom-private'])
        walk=os.walk
        visited=[]
        def tracked_walk(top, **kwargs):
            for item in walk(top, **kwargs):
                visited.append(Path(item[0]).relative_to(workspace).as_posix())
                yield item
        with patch.object(sources.os,'walk',side_effect=tracked_walk):
            files=source.execute('list_files',{'root':'workspace-1'})['files']
        self.assertIn('src/logs/logger.py',files)
        self.assertIn('src/logs',visited)
        self.assertNotIn('artifacts',visited)
        self.assertNotIn('logs',visited)
        self.assertNotIn('custom-private',visited)

    def test_inherited_git_repository_and_index_overrides_cannot_redirect_source(self):
        self.write('app.py','REQUESTED_REPOSITORY = True\n');self.git('add','app.py')
        other=Path(self.temp.name)/'unrelated';other.mkdir()
        subprocess.run(['git','-C',str(other),'init','-q'],check=True,capture_output=True)
        (other/'unrelated.py').write_bytes(b'UNRELATED_PRIVATE_SENTINEL = True\n')
        subprocess.run(['git','-C',str(other),'add','--all'],check=True,capture_output=True)
        injected={'GIT_DIR':str(other/'.git'),'GIT_WORK_TREE':str(other),
                  'GIT_COMMON_DIR':str(other/'.git'),'GIT_INDEX_FILE':str(other/'.git/index'),
                  'GIT_OBJECT_DIRECTORY':str(other/'.git/objects'),
                  'GIT_ALTERNATE_OBJECT_DIRECTORIES':str(other/'.git/objects'),
                  'GIT_NAMESPACE':'unrelated','GIT_CONFIG_COUNT':'1',
                  'GIT_CONFIG_KEY_0':'core.worktree','GIT_CONFIG_VALUE_0':str(other)}
        with patch.dict(os.environ,injected):
            source=self.source(target='staged')
            listing=source.execute('list_files',{})
            self.assertEqual(os.environ['GIT_DIR'],injected['GIT_DIR'])
        self.assertEqual(source.repo_root,self.root)
        self.assertIn('REQUESTED_REPOSITORY',json.dumps(source.packet))
        self.assertNotIn('UNRELATED_PRIVATE_SENTINEL',json.dumps(source.packet))
        self.assertNotIn('unrelated.py',listing['files'])
        self.assertIn('app.py',listing['files'])

    def test_symlink_cannot_read_a_git_ignored_target_in_repository_or_workspace(self):
        self.write('.gitignore','ignored.txt\nlocal-private.txt\n')
        hidden=self.write('local-private.txt','IGNORED_PRIVATE_SENTINEL\n')
        try:(self.root/'alias.py').symlink_to(hidden)
        except (OSError,NotImplementedError):self.skipTest('Symlink creation unavailable')
        source=self.source(workspace_roots=[self.root])
        for root in ['repo','workspace-1']:
            for path in ['local-private.txt','alias.py']:
                with self.subTest(root=root,path=path):
                    self.assertIn('error',source.execute('read_file',{'root':root,'path':path}))
        self.assertNotIn('IGNORED_PRIVATE_SENTINEL',json.dumps(source.packet))
        self.assertNotIn('local-private.txt',source.execute('list_files',{'root':'workspace-1'})['files'])

    def test_manifest_is_written_once_for_collection_and_once_for_a_search(self):
        for i in range(20):self.write('tracked%02d.py'%i,'before = True\n')
        self.commit('many tracked sources')
        for i in range(20):self.write('tracked%02d.py'%i,'after_%02d = True\n'%i)
        writer=sources._write_json
        git=sources._run_git
        with patch.object(sources,'_write_json',wraps=writer) as writes, \
                patch.object(sources,'_run_git',wraps=git) as git_calls:
            source=self.source()
            self.assertEqual(len(source.packet['files']),20)
            self.assertEqual(writes.call_count,1)
            diff_calls=[call for call in git_calls.call_args_list if '--function-context' in call.args[1]]
            self.assertEqual(len(diff_calls),1)
            for i in range(20):self.assertIn('after_%02d'%i,source.packet['diff'])
            for i in range(20):self.write('new-scan%02d.py'%i,'unmatched text\n')
            writes.reset_mock()
            found=source.execute('search_code',{'query':'needle-not-present'})
            self.assertEqual(found['matches'],[])
            self.assertEqual(writes.call_count,1)
            captures=source.diagnostic_sources()
            self.assertGreaterEqual(len(captures),40)
            for item in source.repository_manifest()['sources']:
                self.assertTrue((self.out/item['snapshot_file']).is_file())

    def test_git_timeout_and_unavailable_executable_raise_safe_source_errors(self):
        failures=[subprocess.TimeoutExpired(['git','PRIVATE_COMMAND_SENTINEL'],30,
                                             output=b'PRIVATE_STDOUT',stderr=b'PRIVATE_STDERR'),
                  FileNotFoundError('PRIVATE_EXECUTABLE_PATH')]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__), \
                    patch.object(sources.subprocess,'run',side_effect=failure):
                with self.assertRaises(sources.RepositoryError) as caught:
                    sources._run_git(self.root,['rev-parse','--show-toplevel'])
                self.assertNotIn('PRIVATE',str(caught.exception))
                self.assertIn('could not complete',str(caught.exception))

    def test_private_credentials_and_main_adjudication_outputs_are_not_review_inputs(self):
        names=['.git-credentials','.htpasswd','deployment.env','report_diagnostics.json',
               'adjudication.json','private-decisions.json','adjudication-input.json',
               'nested/adjudication-input-extra.json']
        for name in names:self.write(name,'PRIVATE_MAIN_DECISION_SENTINEL\n')
        safe=['business-decisions.json','deployment.tfvars','.env.example','.env.dev.sample',
              '.env.template','production.env.example']
        for name in safe:self.write(name,'ordinary_config = True\n')
        source=self.source()
        self.assertNotIn('PRIVATE_MAIN_DECISION_SENTINEL',json.dumps(source.packet))
        for name in names:
            with self.subTest(path=name):self.assertIn('error',source.execute('read_file',{'path':name}))
        for name in safe:
            with self.subTest(path=name):self.assertIn('ordinary_config',source.execute('read_file',{'path':name})['text'])

    def test_private_name_policy_also_applies_to_resolved_symlink_targets(self):
        for i,name in enumerate(['.htpasswd','production.env','adjudication.json','report_diagnostics.json']):
            target=self.write(name,'PRIVATE_TARGET_SENTINEL\n')
            try:(self.root/('alias-%d.py'%i)).symlink_to(target)
            except (OSError,NotImplementedError):self.skipTest('Symlink creation unavailable')
        source=self.source()
        for i in range(4):
            self.assertIn('error',source.execute('read_file',{'path':'alias-%d.py'%i}))
        self.assertNotIn('PRIVATE_TARGET_SENTINEL',json.dumps(source.packet))

    def test_collection_failure_retains_the_index_of_already_captured_files(self):
        self.write('app.py','changed_before_diff_failure = True\n')
        git=sources._run_git
        def fail_diff(root,args,**kwargs):
            if '--function-context' in args:raise sources.RepositoryError('safe diff failure')
            return git(root,args,**kwargs)
        with patch.object(sources,'_run_git',side_effect=fail_diff):
            with self.assertRaises(sources.RepositoryError):self.source()
        manifest=json.loads((self.out/'sources/index.json').read_text(encoding='utf-8'))
        self.assertEqual(len(manifest['sources']),1)
        self.assertIn('changed_before_diff_failure',(self.out/manifest['sources'][0]['snapshot_file']).read_text(encoding='utf-8'))

    def test_budget_omitted_deleted_source_is_discoverable_and_readable_from_baseline(self):
        self.write('obsolete.py','first old line\nsecond old line\nthird old line\n')
        self.commit('baseline deleted source')
        (self.root/'obsolete.py').unlink()
        for budget in [0,1]:
            with self.subTest(initial_budget=budget):
                folder=self.out/str(budget);folder.mkdir()
                with patch.object(sources,'MAX_INITIAL_BYTES',budget):
                    source=sources.RepositorySource(self.root,output_dir=folder,workspace_roots=[self.root])
                self.assertEqual(source.packet['files'],[])
                self.assertTrue(any(item['path']=='obsolete.py' for item in source.repository_manifest()['skipped']))
                listed=source.execute('list_files',{})
                self.assertIn('obsolete.py',listed['files'])
                entry=next(item for item in listed['entries'] if item['path']=='obsolete.py')
                self.assertEqual(entry,{'path':'obsolete.py','view':'base','status':'deleted'})
                page=source.execute('read_file',{'path':'obsolete.py','line_count':1})
                self.assertEqual(page['view'],'base')
                self.assertEqual(page['path'],'repo/obsolete.py@base')
                self.assertEqual(page['text'],'first old line\n')
                self.assertTrue(page['has_more'])
                next_page=source.execute('read_file',{'path':'obsolete.py','start_line':page['next_start_line'],'line_count':1,
                                                      'char_offset':page['next_char_offset']})
                self.assertEqual(next_page['text'],'second old line\n')
                self.assertEqual(next_page['source_id'],page['source_id'])
                record=next(item for item in source.repository_manifest()['sources'] if item['source_id']==page['source_id'])
                self.assertEqual(record['view'],'base')
                self.assertEqual((folder/record['snapshot_file']).read_text(encoding='utf-8'),
                                 'first old line\nsecond old line\nthird old line\n')
                self.assertIn('error',source.execute('read_file',{'root':'workspace-1','path':'obsolete.py'}))
                self.assertNotIn('obsolete.py',source.execute('list_files',{'root':'workspace-1'})['files'])

    def test_unomitted_deleted_file_reads_base_for_staged_review(self):
        self.git('rm','caller.py')
        source=self.source(target='staged')
        self.assertIn('repo/caller.py@base',[item['path'] for item in source.packet['files']])
        page=source.execute('read_file',{'path':'caller.py'})
        self.assertEqual(page['view'],'base')
        self.assertIn('from app',page['text'])
        entry=next(item for item in source.execute('list_files',{})['entries'] if item['path']=='caller.py')
        self.assertEqual(entry['status'],'deleted')

    def test_subdirectory_root_and_invalid_base_are_handled(self):
        sub=self.root/'nested';sub.mkdir()
        source=sources.RepositorySource(sub)
        self.assertEqual(source.repo_root,self.root)
        for base in [None,'--help']:
            with self.subTest(base=base),self.assertRaises(sources.RepositoryError):self.source(target='range',base=base)


if __name__=='__main__':unittest.main()
