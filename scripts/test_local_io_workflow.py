"""Project audit-write guarantees, independent of provider parsing."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import atomic_io
import harness_records
import harness_runner
import review
from test_review import RecordFixture,FAKE_KEY

class LocalIOWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def test_progress_failure_is_advisory_and_does_not_discard_response(self):
        fixture=RecordFixture(self.root/"review")
        actual=harness_records.atomic_write_json
        def write(path,value,**options):
            if Path(path).name=="progress.json":raise PermissionError("private progress detail")
            return actual(path,value,**options)
        with patch.object(harness_records,"atomic_write_json",side_effect=write):
            fixture.begin();fixture.end()
        result=fixture.finish()
        self.assertEqual(result["status"],"completed")
        self.assertTrue(result["parse_ok"])
        self.assertEqual(result["progress_warning_count"],1)
        self.assertNotIn("private progress detail",json.dumps(result))
    def test_final_progress_warning_is_retained_in_child_and_aggregate(self):
        fixture=RecordFixture(self.root/"review");fixture.begin()
        actual=harness_records.atomic_write_json
        def write(path,value,**options):
            if Path(path).name=="progress.json":raise PermissionError("private detail")
            return actual(path,value,**options)
        with patch.object(harness_records,"atomic_write_json",side_effect=write):fixture.end()
        result=fixture.finish();child=review.read_json(self.root/"review/calls/0001-review/result.json")
        self.assertIn("progress_warning",child);self.assertEqual(result["progress_warning_count"],1)
    def test_critical_request_commit_retains_redacted_payload_and_does_not_admit_call(self):
        fixture=RecordFixture(self.root/"review")
        actual=atomic_io.os.replace
        def replace(source,destination):
            if Path(destination).name=="request.json":raise PermissionError(13,"private "+FAKE_KEY)
            return actual(source,destination)
        with patch.object(atomic_io,"_IS_WINDOWS",False),patch.object(atomic_io.os,"replace",side_effect=replace):
            with self.assertRaises(PermissionError) as caught:fixture.begin(payload={"messages":[{"content":FAKE_KEY}]})
        self.assertEqual(fixture.records.calls,{})
        details=caught.exception.local_io_error
        retained=self.root/"review/calls/0001-review"/details["retained_temp_file"]
        self.assertTrue(details["payload_complete"])
        self.assertNotIn(FAKE_KEY,retained.read_text(encoding="utf-8"));self.assertIn("[REDACTED]",retained.read_text(encoding="utf-8"))
        journal=(retained.parent/"local_io_errors.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("private ",journal);self.assertNotIn(FAKE_KEY,journal)
    def test_critical_result_commit_failure_is_not_acknowledged_as_recorded(self):
        fixture=RecordFixture(self.root/"review");fixture.begin()
        actual=atomic_io.os.replace
        def replace(source,destination):
            if Path(destination).name=="result.json":raise PermissionError(13,"private result path")
            return actual(source,destination)
        with patch.object(atomic_io,"_IS_WINDOWS",False),patch.object(atomic_io.os,"replace",side_effect=replace):
            with self.assertRaises(PermissionError) as caught:fixture.end()
        call=fixture.records.calls["r1"]
        self.assertFalse(call.get("ended",False))
        self.assertEqual(caught.exception.local_io_error["target_file"],"result.json")
        retained=call["directory"]/caught.exception.local_io_error["retained_temp_file"]
        self.assertTrue(retained.is_file())
    def test_preparation_failure_records_zero_model_attempts(self):
        error=PermissionError(13,"private preparation detail")
        def source(root):
            with patch.object(atomic_io.os,"replace",side_effect=error):
                atomic_io.atomic_write_json(root/"source-index.json",{"source":"retained"},replace_retry_seconds=0)
        with patch.object(harness_runner,"_run_bridge",side_effect=AssertionError("No runtime/model")):
            result=harness_runner.run_harness_workflow(dict(review.read_json(review.DEFAULT_CONFIG.with_name("config.example.json")),ledger_file=str(self.root/"calls.sqlite3")), {}, self.root/"review",route_selector=lambda config,at=None:{"model":"auto","max_tokens":1000},source_factory=source,dry_run=True)
        self.assertEqual(result["status"],"error");self.assertEqual(result["model_calls"],0)
        self.assertNotIn("private preparation detail",json.dumps(result))
        # The critical JSON helper retains serialized material even before a model call.
        self.assertTrue(list((self.root/"review").glob("source-index.json.*.tmp")))
    def test_cli_summary_keeps_advisory_warning_counts(self):
        cfg=self.root/"config.json";cfg.write_text(json.dumps(review.read_json(review.DEFAULT_CONFIG.with_name("config.example.json"))),encoding="utf-8")
        packet=self.root/"packet.json";packet.write_text('{"files":[]}',encoding="utf-8")
        response={"status":"completed","progress_warning":"Progress sidecar warning","progress_warning_count":2,"local_io_error_count":0,"model_calls":1}
        with patch.object(review,"load_credential",return_value=FAKE_KEY),patch.object(harness_runner,"run_harness_workflow",return_value=response),patch("sys.stdout",new_callable=io.StringIO) as output:
            code=review.main(["--config",str(cfg),"--packet",str(packet),"--output-dir",str(self.root/"cli"),"--quiet-progress"])
        self.assertEqual(code,0)
        result=json.loads(output.getvalue());self.assertEqual(result["progress_warning_count"],2);self.assertEqual(result["local_io_error_count"],0)

if __name__=="__main__":unittest.main()
