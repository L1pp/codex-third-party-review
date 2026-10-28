"""Physical wire-audit fixtures; provider parsing belongs to the official SDK."""
import json
from pathlib import Path
import tempfile
import unittest
from harness_records import WireAudit


class WireAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def audit(self,wire,*,key=None,chunks=None,name="call"):
        root=self.root/name;root.mkdir()
        audit=WireAudit(root,key);audit.metadata(200,{"content-type":"text/event-stream; charset=latin-1"})
        raw=wire.encode("utf-8")
        for part in chunks or [raw]:audit.feed(part)
        audit.close()
        return [json.loads(row) for row in (root/"response_stream.jsonl").read_text(encoding="utf-8").splitlines()]
    def test_utf8_crlf_multiline_comments_and_original_lines_are_retained(self):
        wire=': heartbeat\r\nid: event-1\r\nevent: message\r\ndata: {"choices":[{"delta":\r\ndata: {"content":"中文  内容"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        rows=self.audit(wire);lines=[row["line"] for row in rows]
        self.assertIn(": heartbeat",lines);self.assertIn("id: event-1",lines)
        self.assertIn("event: message",lines);self.assertIn('data: {"content":"中文  内容"}}]}',lines)
        self.assertIn("data: [DONE]",lines)
    def test_line_endings_and_significant_spaces_survive_bytewise_input(self):
        for n,ending in enumerate(("\n","\r","\r\n")):
            wire='data: {"choices":[{"delta":{"content":"  spaced  "}}]}'+ending+ending
            raw=wire.encode();rows=self.audit(wire,chunks=[raw[i:i+1] for i in range(len(raw))],name=str(n))
            self.assertIn('"  spaced  "',rows[0]["line"])
            self.assertTrue(rows[0]["event_complete"])
    def test_split_utf8_is_not_replaced_by_the_conflicting_http_charset(self):
        wire='data: {"choices":[{"delta":{"reasoning_content":"中文🙂"}}]}\n\n'
        raw=wire.encode();rows=self.audit(wire,chunks=[raw[:49],raw[49:50],raw[50:51],raw[51:]])
        text=''.join(row["line"] for row in rows)
        self.assertIn("中文🙂",text);self.assertNotIn("�",text)
    def test_unterminated_received_event_is_saved_as_partial(self):
        wire='data: {"choices":[{"delta":{"reasoning_content":"received before disconnect"}}]}'
        rows=self.audit(wire)
        self.assertIn("received before disconnect",rows[0]["line"])
        self.assertFalse(rows[0]["event_complete"])
    def test_credential_split_between_reasoning_and_content_events_is_redacted(self):
        key="sk-cross-event-audit-secret"
        events=[{"choices":[{"delta":{"reasoning_content":key[:10]}}]},
                {"choices":[{"delta":{"content":key[10:]}}]}]
        wire=''.join('data: '+json.dumps(event)+'\n\n' for event in events)
        rows=self.audit(wire,key=key)
        text=json.dumps(rows)
        self.assertNotIn(key,text);self.assertNotIn(key[:10],text);self.assertIn("[REDACTED]",text)

if __name__=="__main__":unittest.main()
