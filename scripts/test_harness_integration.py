"""Offline end-to-end contract tests through the real official Node runtime.

A preloader replaces fetch before any provider is constructed; no network/model
request is possible. Repository material, protocol parsing and logs are real.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import harness_runner
import ledger
import review
from review_resume import load_resume

ROOT=Path(__file__).resolve().parent.parent
ROUTE={"model":"deepseek-v4.1-flash-1017","reasoning_effort":"max","enable_thinking":True,"max_tokens":256000,"context_window_tokens":1000000}
PACKET={"task":"Review calculate without editing files.","files":[{"path":"calc.py","content":"def calculate(x):\n    return x + 1\n"}]}
KEY="offline-integration-secret-placeholder"

def event(content=None, reason=None, calls=None, finish=None):
    delta={}
    if content is not None:delta["content"]=content
    if reason is not None:delta["reasoning_content"]=reason
    if calls is not None:delta["tool_calls"]=calls
    choice={"index":0,"delta":delta}
    if finish is not None:choice["finish_reason"]=finish
    return {"model":ROUTE["model"],"choices":[choice]}

def final():
    return {"events":[event(content='{"findings":[],"context_gaps":[]}',finish="stop"),{"choices":[],"usage":{"prompt_tokens":40,"completion_tokens":12,"completion_tokens_details":{"reasoning_tokens":2}}},"[DONE]"]}

class OfficialHarnessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from node_runtime import node_runtime_info
        cls.ready=node_runtime_info(ROOT)
        if not cls.ready.get("ready"):
            raise unittest.SkipTest("Run the explicit Skill setup to install the official Node runtime")
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.out=self.home/"review"
        self.config=review.read_json(ROOT/"config.example.json")
        self.config.update(base_url="https://offline.example.invalid/v1",ledger_file=str(self.home/"ledger.sqlite3"))
        self.config["context_management"]["inline_material_token_limit"]=0
        self.config.pop("model_catalog_file",None)
        self.config["api_recovery"]={"retry_window_seconds":0}
        handle=patch.object(harness_runner,"BRIDGE",ROOT/"runtime/fixtures/offline_bridge.mjs")
        handle.start();self.addCleanup(handle.stop)
    def run_case(self,steps,*,output=None,resume=None,packet=None,route=None):
        scenario=self.home/("scenario-%d.json"%len(list(self.home.glob("scenario-*.json"))))
        scenario.write_text(json.dumps({"steps":steps}),encoding="utf-8")
        with patch.dict(os.environ,{"CODEX_REVIEW_TEST_SCENARIO":str(scenario)}),patch.object(review,"load_credential",return_value=KEY):
            kwargs={"resume_state":resume,"source_factory":resume.source_factory(self.config)} if resume else {}
            return harness_runner.run_harness_workflow(self.config,copy.deepcopy(PACKET if packet is None else packet),output or self.out,route_selector=lambda cfg,at=None:ROUTE if route is None else route,**kwargs)
    def test_real_loop_protocol_tool_result_and_complete_report(self):
        calls=[{"index":0,"id":"read-1","type":"function","function":{"name":"read_material","arguments":json.dumps({"path":"calc.py","start_line":1,"line_count":2})}}]
        result=self.run_case([{"events":[event(reason="Read the source before judging.",calls=calls,finish="tool_calls"),"[DONE]"]},final()])
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["parse_ok"])
        self.assertEqual(result["model_calls"],2)
        self.assertEqual(result["tools_executed"],1)
        self.assertTrue((self.out/"engine-session/session.jsonl").is_file())
        self.assertTrue((self.out/"tool_results.jsonl").is_file())
        self.assertEqual(len(ledger.history(Path(self.config["ledger_file"]))["records"]),2)
        self.assertIn("data: [DONE]",(self.out/"calls/0001-review/response_stream.jsonl").read_text(encoding="utf-8"))
    def test_official_retry_preserves_failure_and_makes_new_attempt(self):
        self.config["api_recovery"]={"retry_window_seconds":2}
        result=self.run_case([{"status":503,"headers":{"content-type":"application/json","retry-after":"0.001"},"body":json.dumps({"error":{"code":"ServiceUnavailable","message":"RESOURCE_EXHAUSTED"}})},final()])
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(result["model_calls"],2)
        self.assertEqual(result["child_calls"][0]["status"],"error")
        self.assertEqual(result["child_calls"][1]["status"],"completed")
        self.assertGreater(result["timing"]["failed_request_seconds"],0)
        self.assertGreater(result["timing"]["retry_wait_seconds"],0)
    def test_real_deadline_progress_uses_official_checkpoint_then_continues(self):
        thought="Check the source and retain the uncertain finding with evidence. "*200
        failed={"events":[event(reason=thought),{"error":{"code":"GatewayTimeout","message":"Deadline Exceeded"}}]}
        summary={"expectedPurpose":"compaction","events":[event(content="Checkpoint: inspect calculate and verify the unresolved input boundary.",finish="stop"),"[DONE]"]}
        result=self.run_case([failed,summary,final()])
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(result["model_calls"],3)
        self.assertEqual(result["compactions"],1)
        self.assertEqual(result["child_calls"][0]["status"],"error")
        self.assertGreater(result["timing"]["failed_request_seconds"],0)
        self.assertTrue((self.out/"checkpoint.txt").exists())
        self.assertIn("Checkpoint:",(self.out/"checkpoint.txt").read_text(encoding="utf-8"))
    def test_single_request_uses_same_official_protocol_without_tool_execution(self):
        scenario=self.home/"single.json";scenario.write_text(json.dumps({"steps":[final()]}),encoding="utf-8")
        with patch.dict(os.environ,{"CODEX_REVIEW_TEST_SCENARIO":str(scenario)}),patch.object(review,"load_credential",return_value=KEY):
            result=review.run_review(self.config,copy.deepcopy(PACKET),self.out,route_override=ROUTE)
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["parse_ok"])
        self.assertTrue((self.out/"request.json").is_file())
        self.assertFalse((self.out/"calls").exists())
        self.assertEqual(len(ledger.history(Path(self.config["ledger_file"]))["records"]),1)

    def test_orphan_engine_header_uses_the_loader_legacy_progress_choice(self):
        self.run_case([{"status":401,"headers":{"content-type":"application/json"},"body":json.dumps({"error":{"code":"invalid_api_key","message":"offline prior request"}})}])
        (self.out/"engine-session/session.jsonl").unlink()
        state=load_resume(self.out)
        from harness_runner import build_engine_spec
        spec=build_engine_spec(self.config,self.home/"next",state.route,[{"role":"user","content":"prepared source"}],[],state)
        self.assertNotIn("resume",spec)
        self.assertIn("legacyProgress",spec)
        result=self.run_case([final()],output=self.home/"orphan-resumed",resume=state)
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(result["model_calls"],1)
        self.assertFalse((self.out/"engine-session/session.jsonl").exists())

    def test_failed_initial_compaction_only_record_can_resume_the_original_review(self):
        route={"model":"auto","max_tokens":8000,"context_window_tokens":20000}
        self.config["context_management"].update(inline_material_token_limit=100000,headroom_tokens=1000,compaction_max_tokens=512,retain_recent_tokens=0)
        packet={"task":"Review the supplied snapshot.","files":[{"path":"large.py","content":"x=1;"*3000}]}
        failed=self.run_case([{"status":401,"headers":{"content-type":"application/json"},"body":json.dumps({"error":{"code":"invalid_api_key","message":"offline initial summary failure"}})}],packet=packet,route=route)
        self.assertNotEqual(failed["status"],"completed",failed)
        self.assertTrue(failed["child_calls"],failed)
        self.assertTrue(all(call["stage"]=="compaction" for call in failed["child_calls"]))
        state=load_resume(self.out)
        self.assertEqual(state.route["max_tokens"],8000)
        self.assertFalse(state.provenance["review_progress_available"])
        checkpoint={"events":[event(content="CHECKPOINT: the supplied snapshot remains to be checked.",finish="stop"),"[DONE]"]}
        result=self.run_case([checkpoint,final()],output=self.home/"summary-resumed",resume=state,route=route)
        if result["status"]!="completed":
            flow=json.loads((self.home/"summary-resumed/workflow.json").read_text(encoding="utf-8"))
            self.fail(json.dumps({"status":result["status"],"reason":result.get("uncompleted_reason"),"preflight":flow.get("runtime_preflight_errors")},ensure_ascii=False))
        self.assertEqual(result["route"]["max_tokens"],8000)
        self.assertTrue((self.out/"calls/0001-compaction/result.json").exists())

    def test_unclosed_node_record_can_restore_saved_progress_without_a_result(self):
        self.run_case([{"status":401,"headers":{"content-type":"application/json"},"body":json.dumps({"error":{"code":"invalid_api_key","message":"offline prior request"}})}])
        flow_path=self.out/"workflow.json";flow=json.loads(flow_path.read_text(encoding="utf-8"));flow["status"]="running";flow_path.write_text(json.dumps(flow),encoding="utf-8")
        call=self.out/"calls/0001-review";(call/"result.json").unlink()
        (call/"reasoning.txt").write_text("SAVED_PROGRESS_BEFORE_PROCESS_INTERRUPTION",encoding="utf-8")
        state=load_resume(self.out)
        self.assertTrue(state.crash_recovery_candidate)
        saved=(self.out/"engine-session/session.jsonl").read_bytes()
        result=self.run_case([final()],output=self.home/"crash-resumed",resume=state)
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(result["model_calls"],1)
        request=(self.home/"crash-resumed/calls/0001-review/request.json").read_text(encoding="utf-8")
        self.assertIn("SAVED_PROGRESS_BEFORE_PROCESS_INTERRUPTION",request)
        self.assertFalse((call/"result.json").exists())
        self.assertEqual((self.out/"engine-session/session.jsonl").read_bytes(),saved)

    def test_new_record_resume_uses_official_session_without_replaying_tools(self):
        first=self.run_case([{"status":401,"headers":{"content-type":"application/json"},"body":json.dumps({"error":{"code":"invalid_api_key","message":"offline authentication"}})}])
        self.assertNotEqual(first["status"],"completed")
        saved=(self.out/"engine-session/session.jsonl").read_bytes()
        state=load_resume(self.out)
        self.config["context_management"]["inline_material_token_limit"]=100000
        result=self.run_case([final()],output=self.home/"resumed",resume=state)
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(result["model_calls"],1)
        self.assertEqual(result["tools_executed"],0)
        self.assertEqual((self.out/"engine-session/session.jsonl").read_bytes(),saved)
        request=json.loads((self.home/"resumed/calls/0001-review/request.json").read_text(encoding="utf-8"))
        self.assertTrue(request["payload"].get("tools"))
        self.assertEqual(result["child_calls"][0]["inline_material_ranges"],[])
        self.assertIn("materials_index",request["payload"]["messages"][-1]["content"])

if __name__=="__main__":unittest.main()
