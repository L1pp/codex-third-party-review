"""Repository contracts through the real official loop and an offline fetch fixture."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import harness_runner
import review
from node_runtime import node_runtime_info
from repository_source import RepositorySource
from test_harness_integration import event, final, ROUTE, KEY
ROOT=Path(__file__).resolve().parent.parent
EXAMPLE=review.read_json(ROOT/"config.example.json")
NIGHT="2026-10-27T23:00:00+08:00"
class RepositoryWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ready=node_runtime_info(ROOT)
        if not cls.ready.get("ready"):raise unittest.SkipTest("Explicit Skill setup required for Node fixtures")
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve();self.repo=self.home/"repo";self.repo.mkdir();self.out=self.home/"review"
        self.git("init","-q")
        (self.repo/"app.py").write_text("answer = 1\n",encoding="utf-8")
        (self.repo/"helper.py").write_text("UNCHANGED_HELPER = 42\n",encoding="utf-8")
        self.git("add",".");self.git("commit","-qm","baseline")
        (self.repo/"app.py").write_text("from helper import UNCHANGED_HELPER\nanswer = UNCHANGED_HELPER\n",encoding="utf-8")
        self.config=json.loads(json.dumps(EXAMPLE));self.config.update(base_url="https://offline.example.invalid/v1",ledger_file=str(self.home/"calls.sqlite3"))
        self.config["api_recovery"]={"retry_window_seconds":0};self.config.pop("model_catalog_file",None)
        handle=patch.object(harness_runner,"BRIDGE",ROOT/"runtime/fixtures/offline_bridge.mjs");handle.start();self.addCleanup(handle.stop)
    def git(self,*args):
        return subprocess.run(["git","-c","user.name=Offline fixture","-c","user.email=offline@example.invalid","-c","commit.gpgsign=false","-c","core.hooksPath="+str(self.home/"no-hooks"),"-C",str(self.repo),*args],check=True,capture_output=True).stdout
    def run_source(self,name,args,**source_options):
        calls=[{"index":0,"id":"read-source","type":"function","function":{"name":name,"arguments":json.dumps(args)}}]
        scenario=self.home/"scenario.json";scenario.write_text(json.dumps({"steps":[{"events":[event(calls=calls,finish="tool_calls"),"[DONE]"]},final()]}),encoding="utf-8")
        with patch.dict(os.environ,{"CODEX_REVIEW_TEST_SCENARIO":str(scenario)}),patch.object(review,"load_credential",return_value=KEY):
            return harness_runner.run_harness_workflow(self.config,{},self.out,route_selector=lambda cfg,at=None:ROUTE,source_factory=lambda root:RepositorySource(self.repo,task="Review caller compatibility",output_dir=root,**source_options))
    def test_inline_change_can_explore_unchanged_source(self):
        result=self.run_source("read_file",{"root":"repo","path":"helper.py"})
        self.assertEqual(result["status"],"completed",result);self.assertEqual(result["tools_executed"],1)
        request=json.loads((self.out/"calls/0002-review/request.json").read_text(encoding="utf-8"))
        self.assertIn("UNCHANGED_HELPER",json.dumps(request["payload"]["messages"]))
        self.assertTrue((self.out/"sources/index.json").exists())
        self.assertTrue(result["coverage"]["read_sources"][0]["source_id"])
    def test_staged_source_keeps_index_view_when_disk_changes(self):
        self.git("add","app.py");(self.repo/"app.py").write_text("DISK_ONLY_DIFFERENCE = 1\n",encoding="utf-8")
        result=self.run_source("read_file",{"root":"repo","path":"app.py"},target="staged")
        self.assertEqual(result["status"],"completed",result)
        req=json.loads((self.out/"calls/0002-review/request.json").read_text(encoding="utf-8"))
        self.assertNotIn("DISK_ONLY_DIFFERENCE",json.dumps(req["payload"]["messages"]))
        self.assertTrue(any(item.get("view")=="staged" for item in result["coverage"]["read_sources"]))
    def test_workspace_source_is_a_separate_recorded_view(self):
        workspace=self.home/"shared";workspace.mkdir();(workspace/"contract.py").write_text("SHARED_CONTRACT = 7\n",encoding="utf-8")
        result=self.run_source("read_file",{"root":"workspace-1","path":"contract.py"},workspace_roots=[workspace])
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(any(item.get("root")=="workspace-1" for item in result["coverage"]["read_sources"]))
    def test_cli_default_repository_and_dry_run_never_launch_node(self):
        import io
        from contextlib import redirect_stdout
        cfg=self.home/"config.json";cfg.write_text(json.dumps(self.config),encoding="utf-8")
        output=io.StringIO()
        with patch.object(review.Path,"cwd",return_value=self.repo),patch.object(review,"load_credential",side_effect=AssertionError("No key")),patch.object(harness_runner,"_run_bridge",side_effect=AssertionError("No runtime")),redirect_stdout(output):
            code=review.main(["--config",str(cfg),"--output-dir",str(self.out),"--dry-run","--at",NIGHT])
        self.assertEqual(code,0,output.getvalue());self.assertEqual(json.loads(output.getvalue())["status"],"dry_run")
    def test_runtime_session_sidecars_are_not_repository_materials(self):
        path=self.repo/"session-header.json";path.write_text("PRIVATE_RUNTIME_MATERIAL",encoding="utf-8")
        result=self.run_source("read_file",{"root":"repo","path":"session-header.json"})
        self.assertEqual(result["status"],"completed",result)
        req=(self.out/"calls/0002-review/request.json").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_RUNTIME_MATERIAL",req)
        self.assertIn("excluded",req.lower())
if __name__=="__main__":unittest.main()
