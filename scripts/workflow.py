"""Compatibility imports for the review host; no model or tool loop lives here.

The only live driver is the official DeepSeek Harness AgentLoop in runtime/engine.mjs.
"""
try:
    from .harness_records import _positive_limit, _messages_for_review, _scope_only, _save_outputs
    from .harness_runner import run_harness_workflow
except ImportError:
    from harness_records import _positive_limit, _messages_for_review, _scope_only, _save_outputs
    from harness_runner import run_harness_workflow


def run_workflow(config, packet, output_dir, *, review_call=None, **options):
    if review_call is not None:
        raise ValueError("Python provider-loop injection was retired; use the official runtime adapter or an offline fetch fixture")
    return run_harness_workflow(config, packet, output_dir, **options)
