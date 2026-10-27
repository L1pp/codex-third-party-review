"""Transport edge cases informed by the upstream CLI SSE boundary handling.

These tests use requests' real UTF-8 decoder, not a provider or network service.
"""
import io
import json
import unittest
import requests

import review


class ByteFragments(io.BytesIO):
    def read(self, amount=-1):
        return super().read(1 if amount < 0 else min(amount, 1))


def response_for(text):
    response = requests.Response()
    response.status_code = 200
    response.encoding = "utf-8"
    response.raw = ByteFragments(text.encode("utf-8"))
    return response


class SSETransportTests(unittest.TestCase):
    def test_utf8_crlf_multiline_events_preserve_text_and_raw_lines(self):
        text = ': heartbeat\r\nid: event-1\r\nevent: message\r\ndata: {"choices":[{"delta":\r\ndata: {"content":"中文  内容"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        lines = []
        events = list(review.sse_events(response_for(text), on_line=lines.append))
        self.assertEqual(json.loads(events[0])["choices"][0]["delta"]["content"], "中文  内容")
        self.assertEqual(events[-1], "[DONE]")
        self.assertIn(": heartbeat", lines)
        self.assertIn("id: event-1", lines)
        self.assertIn("event: message", lines)

    def test_line_ending_styles_and_meaningful_spaces_are_preserved(self):
        for ending in ("\n", "\r", "\r\n"):
            with self.subTest(ending=repr(ending)):
                lines = []
                calls = []
                text = "data:   leading  spaces" + ending + "data:\tvalue" + ending * 2
                events = list(review.sse_events(response_for(text), on_line=lines.append,
                    on_event=lambda value, complete: calls.append((value, complete))))
                self.assertEqual(events, ["  leading  spaces\n\tvalue"])
                self.assertEqual(lines, ["data:   leading  spaces", "data:\tvalue", ""])
                self.assertEqual(calls, [(events[0], True)])

    def test_sse_uses_utf8_even_with_a_conflicting_http_encoding(self):
        response = response_for('data: {"value":"中文"}\r\n\r\n')
        response.encoding = "latin-1"
        events = list(review.sse_events(response))
        self.assertEqual(json.loads(events[0]), {"value": "中文"})

    def test_received_partial_line_is_logged_before_transport_error(self):
        class InterruptedFragments(ByteFragments):
            def read(self, amount=-1):
                chunk = super().read(amount)
                if not chunk:
                    raise requests.ReadTimeout("test interruption")
                return chunk
        text = 'data: {"choices":[{"delta":{"reasoning_content":"已接收"}}]}'
        response = response_for(text)
        response.raw = InterruptedFragments(text.encode("utf-8"))
        lines = []
        events = []
        with self.assertRaises(requests.ReadTimeout):
            for event in review.sse_events(response, on_line=lines.append):
                events.append(event)
        self.assertEqual(lines, [text])
        self.assertEqual(events, [])  # A transport error does not complete the event.

    def test_unterminated_utf8_event_is_available_and_marked_partial(self):
        calls = []
        text = '\ufeffdata: {"choices":[{"delta":{"reasoning_content":"未结束的思考"}}]}'
        events = list(review.sse_events(response_for(text), on_event=lambda value, complete: calls.append((value, complete))))
        self.assertEqual(json.loads(events[0])["choices"][0]["delta"]["reasoning_content"], "未结束的思考")
        self.assertEqual(calls[-1][1], False)
        self.assertNotIn("[DONE]", events)


if __name__ == "__main__":
    unittest.main()
