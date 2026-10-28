"""Low-volume CLI status on stderr; never part of review materials."""
import sys
import time


class ReviewProgress:
    def __init__(self, stream=None, interval=60, clock=None):
        self.stream = sys.stderr if stream is None else stream
        self.interval = interval
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.last = None
        self.available = True

    def _write(self, text):
        if not self.available:
            return
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except Exception:
            self.available = False  # Status output must not fail a review.

    def __call__(self, event):
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            return
        self.last = now
        phase = ("report" if event.get("visible_chars", 0) else
                 "reasoning" if event.get("reasoning_chars", 0) else event.get("state", "waiting"))
        call = str(event.get("call", "review")).replace("\n", " ")[:64]
        self._write("[review] elapsed=%ds call=%s phase=%s reasoning_chars=%d text_chars=%d" %
                    (now - self.started, call, phase, event.get("reasoning_chars", 0), event.get("visible_chars", 0)))

    def finish(self, result):
        self._write("[review] finished=%s elapsed=%ds model_calls=%d tools=%d" %
                    (result.get("status", "unknown"), self.clock() - self.started,
                     result.get("model_calls", 0) or 0, result.get("tools_executed", 0) or 0))
