"""Project workflow contracts around the maintained official driver.

Agent-loop, retry and compaction internals are exercised by runtime/test_*.mjs.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import workflow
from test_review import RecordFixture


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"review"
    def test_legacy_python_provider_loop_injection_is_rejected_before_side_effects(self):
        with self.assertRaisesRegex(ValueError,"retired"):
            workflow.run_workflow({}, {}, self.root, review_call=lambda *a,**k:self.fail("No legacy loop"))
        self.assertFalse(self.root.exists())
    def test_compatibility_entry_delegates_to_the_single_official_driver(self):
        with patch.object(workflow,"run_harness_workflow",return_value={"status":"dry_run"}) as run:
            self.assertEqual(workflow.run_workflow({}, {}, self.root,route_selector="selector",dry_run=True),{"status":"dry_run"})
        self.assertEqual(run.call_args.args,({}, {}, self.root));self.assertEqual(run.call_args.kwargs["route_selector"],"selector")
    def test_failed_nonempty_continuation_does_not_replace_an_earlier_real_partial(self):
        fixture=RecordFixture(self.root);fixture.begin("first")
        partial='{"findings":[{"issue":"Earlier real finding"}]}'
        fixture.end(partial,ident="first",status="incomplete",finish="length")
        fixture.begin("failed");fixture.end("short failed fragment",ident="failed",status="error",finish=None,error_type="ProviderError")
        result=fixture.finish("error")
        self.assertEqual(result["content"],partial);self.assertTrue(result["content_is_partial"])
        self.assertEqual(result["status"],"error")
        self.assertEqual(json.loads((self.root/"calls/0002-review/result.json").read_text(encoding="utf-8"))["content"],"short failed fragment")
    def test_success_failure_length_and_wait_timings_are_separate(self):
        fixture=RecordFixture(self.root)
        for ident,status,finish,seconds in [("ok","completed","stop",1),("failed","error",None,2),("length","incomplete","length",3)]:
            fixture.begin(ident);fixture.end(ident=ident,status=status,finish=finish,seconds=seconds)
        fixture.workflow["retry_wait_seconds"]=4
        result=fixture.finish("incomplete")
        self.assertEqual(result["timing"]["successful_request_seconds"],1)
        self.assertEqual(result["timing"]["failed_request_seconds"],2)
        self.assertEqual(result["timing"]["incomplete_request_seconds"],3)
        self.assertEqual(result["timing"]["retry_wait_seconds"],4)
    def test_unseen_inline_attempt_is_not_claimed_as_confirmed_delivery(self):
        fixture=RecordFixture(self.root)
        fixture.records.bundle={"delivered_ranges":[{"path":"sample.py","line_numbers":[1]}]}
        fixture.begin();fixture.end("",status="error",finish=None,reasoning_chars=0,tool_calls=[])
        result=fixture.finish("error")
        self.assertEqual(result["coverage"]["inline_delivered_ranges"],[])
        self.assertEqual(len(result["coverage"]["inline_delivery_requests"]),1)
    def test_tool_narrative_is_not_promoted_to_a_report_after_failure(self):
        fixture=RecordFixture(self.root)
        fixture.begin("tool");fixture.end("I will inspect more source",ident="tool",status="tool_calls",finish="tool_calls",tool_calls=[{"id":"t1"}])
        fixture.begin("failure");fixture.end("",ident="failure",status="error",finish=None)
        result=fixture.finish("error")
        self.assertEqual(result["content"],"");self.assertFalse(result["parse_ok"])
        self.assertNotIn("I will inspect more source",(self.root/"report.md").read_text(encoding="utf-8"))
    def test_only_successful_tool_results_contribute_actual_source_ranges(self):
        fixture=RecordFixture(self.root)
        good=fixture.records.record_tool(fixture.store,{"name":"read_material","arguments":{"path":"sample.py"},"callId":"t1"})
        bad=fixture.records.record_tool(fixture.store,{"name":"read_material","arguments":{"path":"missing.py"},"callId":"t2"})
        self.assertIn("value",good["text"]);self.assertIn("error",bad)
        self.assertEqual(len(fixture.records.read_sources),1)
        rows=[json.loads(line) for line in (self.root/"tool_results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows),2);self.assertEqual(rows[1]["result"],bad)

if __name__=="__main__":unittest.main()
