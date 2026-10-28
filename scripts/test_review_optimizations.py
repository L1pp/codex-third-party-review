"""Offline review contracts, supplements and low-volume progress."""
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import harness_records
import harness_runner
import review
import workflow
import report_diagnostics
from repository_source import RepositorySource,RepositoryError
from review_contract import SYSTEM_PROMPT
from review_progress import ReviewProgress
from test_review import RecordFixture,CONFIG,FAKE_KEY

class ContractTests(unittest.TestCase):
    def test_direct_and_workflow_requests_use_the_same_contract(self):
        direct=review.make_payload({"task":"test"},{"model":"offline"})["messages"][0]["content"]
        orchestrated=workflow._messages_for_review({"task":"test"},{})[0]["content"]
        self.assertEqual(direct,SYSTEM_PROMPT)
        self.assertEqual(orchestrated,direct)
        self.assertTrue(all(p in orchestrated for p in ("P0","P1","P2","P3")))

    def test_known_priority_aliases_are_derived_without_mutating_originals(self):
        priorities=["high","medium","low","高","中","低","P0"]
        findings=[{"priority":p,"file":"x.py","line":1,"issue":"check","basis":"source","uncertainty":"verify"} for p in priorities]
        before=copy.deepcopy(findings)
        result=report_diagnostics.analyze_report(json.dumps({"findings":findings}),sources=[])
        self.assertEqual([r["normalized_priority"] for r in result["findings"]],["P1","P2","P3","P1","P2","P3","P0"])
        self.assertTrue(all(r["fields"]["valid"] for r in result["findings"]))
        self.assertEqual(findings,before)
        self.assertEqual([r["original"] for r in result["findings"]],before)

    def test_progress_is_rate_limited_and_never_prints_source_material(self):
        now=[0];stream=io.StringIO();emit=ReviewProgress(stream=stream,clock=lambda:now[0])
        event={"call":"0001-review","state":"streaming","visible_chars":0,"reasoning_chars":200,"content":"PRIVATE_SOURCE"}
        emit(event)
        now[0]=30;emit(event)
        now[0]=60;emit(dict(event,reasoning_chars=500))
        now[0]=65;emit(event)
        self.assertEqual(len(stream.getvalue().splitlines()),2)
        self.assertNotIn("PRIVATE_SOURCE",stream.getvalue())
        self.assertIn("phase=reasoning",stream.getvalue())
        emit.finish({"status":"completed","model_calls":2,"tools_executed":3})
        self.assertEqual(len(stream.getvalue().splitlines()),3)

    def test_progress_sink_failure_is_non_blocking(self):
        class Broken:
            def write(self,text):raise OSError("closed sink")
        emit=ReviewProgress(stream=Broken())
        emit({"state":"requesting"});emit.finish({"status":"completed"})
        self.assertFalse(emit.available)

    def test_transport_callback_reports_counts_without_materials(self):
        with tempfile.TemporaryDirectory() as temp:
            events=[]
            fixture=RecordFixture(Path(temp)/"call",label="Trace an actual packet title",callback=events.append)
            fixture.begin(payload={"messages":[{"content":"PRIVATE_SOURCE"}]})
            fixture.records.response_event("response.reasoning",{"id":"r1","text":"private reasoning "+FAKE_KEY})
            fixture.end()
            self.assertTrue(events)
            self.assertNotIn("PRIVATE_SOURCE",json.dumps(events));self.assertNotIn(FAKE_KEY,json.dumps(events))
            self.assertTrue(all(set(row)<={"state","call","reasoning_chars","visible_chars"} for row in events))
            self.assertEqual(review.ledger.history(fixture.config["ledger_file"])["records"][0]["task"],"Trace an actual packet title")

class SupplementalSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.repo=self.home/"repo";self.repo.mkdir()
        self.git("init","-q")
        (self.repo/"app.py").write_text("value = 1\n",encoding="utf-8")
        (self.repo/"helper.py").write_text("def add(a, b):\n    return a + b\n",encoding="utf-8")
        self.git("add",".");self.git("commit","-qm","baseline")
        self.out=self.home/"review";self.config=copy.deepcopy(CONFIG)
        self.config["ledger_file"]=str(self.home/"calls.sqlite3")
        (self.repo/".gitignore").write_text("artifacts/\n",encoding="utf-8")
        (self.repo/"artifacts").mkdir()
        (self.repo/"artifacts/deploy.py").write_text("from helper import add\n# EXPLICIT_DEPLOY\n",encoding="utf-8")
        (self.repo/"artifacts/private.txt").write_text("PRIVATE_SIBLING",encoding="utf-8")
    def git(self,*args):
        return subprocess.run(["git","-c","user.name=Offline fixture","-c","user.email=fixture@example.invalid","-c","commit.gpgsign=false","-c","core.hooksPath="+str(self.home/"no-hooks"),"-C",str(self.repo),*args],check=True,capture_output=True).stdout

    def test_supplement_is_visible_without_opening_its_directory(self):
        source=RepositorySource(self.repo,include_files=['artifacts/deploy.py'],execution_context=['prepare runs on Windows; deploy runs on Linux'])
        self.assertIn('supplement',source.roots)
        selected=source.execute('read_file',{'root':'supplement','path':'artifacts/deploy.py'})
        self.assertIn('EXPLICIT_DEPLOY',selected['text'])
        self.assertEqual(selected['view'],'worktree')
        self.assertIn('error',source.execute('read_file',{'root':'supplement','path':'artifacts/private.txt'}))
        self.assertIn('error',source.execute('read_file',{'root':'repo','path':'artifacts/deploy.py'}))
        files=source.execute('list_files',{'root':'supplement'})
        self.assertEqual(files['files'],['artifacts/deploy.py'])
        self.assertIn('return a + b',source.execute('read_file',{'path':'helper.py'})['text'])
        self.assertEqual(source.packet['scope']['execution_context'],['prepare runs on Windows; deploy runs on Linux'])

    def test_supplement_does_not_replace_staged_version(self):
        (self.repo/'app.py').write_bytes(b'from helper import add\nvalue=add(2,3)\n')
        self.git('add','--','app.py')
        staged=(self.repo/'app.py').read_text(encoding='utf-8')
        (self.repo/'app.py').write_bytes(b'value = 999\n')
        source=RepositorySource(self.repo,target='staged',include_files=['app.py'])
        self.assertEqual(source.execute('read_file',{'path':'app.py'})['text'],staged)
        self.assertEqual(source.execute('read_file',{'root':'supplement','path':'app.py'})['text'],'value = 999\n')

    def test_explicit_file_keeps_existing_credential_and_output_exclusions(self):
        (self.repo/'artifacts/.env').write_text('SECRET',encoding='utf-8')
        for path in ['artifacts/.env','artifacts']:
            with self.subTest(path=path):
                with self.assertRaises(RepositoryError):RepositorySource(self.repo,include_files=[path])

    def test_cli_mixed_mode_passes_facts_and_repo_tools_without_a_model(self):
        cfg=self.home/"config.json";cfg.write_text(json.dumps(self.config),encoding="utf-8")
        with patch.object(review,"load_credential",side_effect=AssertionError("No credentials")), \
             patch.object(harness_runner,"_run_bridge",side_effect=AssertionError("No runtime/model")), \
             patch.object(harness_runner,"_ensure_runtime",side_effect=AssertionError("No dependency install")), \
             patch("sys.stdout",new_callable=io.StringIO):
            code=review.main(["--config",str(cfg),"--repo",str(self.repo),"--include-file","artifacts/deploy.py","--execution-context","SCP transfers the prepared file before deployment","--output-dir",str(self.out),"--dry-run"])
        self.assertEqual(code,0)
        result=json.loads((self.out/"result.json").read_text(encoding="utf-8"));self.assertEqual(result["model_calls"],0)
        request=json.loads((self.out/"calls/0001-review/request.json").read_text(encoding="utf-8"))["payload"]
        self.assertIn("read_file",{tool["function"]["name"] for tool in request["tools"]})
        packet=json.loads((self.out/"materials.json").read_text(encoding="utf-8"))
        self.assertTrue(any(row["path"]=="supplement/artifacts/deploy.py" for row in packet["files"]))
        self.assertIn("SCP transfers",json.dumps(request["messages"]))

if __name__=="__main__":unittest.main()
