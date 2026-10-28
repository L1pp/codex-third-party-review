"""Pressure-only history cleanup; originals and source snapshots stay on disk.

Inspired by DSH compaction-basic and compaction-tool-result-pruner. This module
has no provider, filesystem, shell or retry operations.
"""
import json

try:
    from .context_budget import estimate_tokens
except ImportError:
    from context_budget import estimate_tokens


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _history_groups(messages):
    """Split only at complete assistant/tool boundaries; reject unsafe shapes."""
    first = next((i for i, m in enumerate(messages) if m.get("role") == "assistant"), len(messages))
    header = messages[:first]
    groups = []
    index = first
    while index < len(messages):
        message = messages[index]
        if message.get("role") != "assistant":
            return messages, []
        group = [message]
        calls = message.get("tool_calls") or []
        ids = [call.get("id") for call in calls if isinstance(call, dict)]
        if len(ids) != len(calls) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            return messages, []
        index += 1
        returned = []
        while index < len(messages) and messages[index].get("role") == "tool":
            group.append(messages[index])
            returned.append(messages[index].get("tool_call_id"))
            index += 1
        if len(ids) != len(returned) or set(ids) != set(returned):
            return messages, []
        groups.append(group)
    return header, groups


def split_history(messages, retain_tokens, model, *, require_older=True):
    """Return older messages and a verbatim recent tail with balanced tools.

    At least one old turn is selected for summary when pressure requires one.
    A single turn larger than the retention budget is summarized as a whole.
    """
    header, groups = _history_groups(messages)
    if not groups or (require_older and len(groups) < 2) or retain_tokens <= 0:
        return list(messages), []
    kept = []
    cost = 0
    for group in reversed(groups[1:] if require_older else groups):
        group_cost = estimate_tokens(group, [], model)["input_tokens"]
        if cost + group_cost > retain_tokens:
            break
        kept.insert(0, group)
        cost += group_cost
    flat = [message for group in kept for message in group]
    # Group-wise estimates can differ from the combined chat template.
    while kept and estimate_tokens(flat, [], model)["input_tokens"] > retain_tokens:
        kept.pop(0)
        flat = [message for group in kept for message in group]
    cut = len(groups) - len(kept)
    return header + [message for group in groups[:cut] for message in group], flat


def prune_old_reads(messages, retain_tokens, model):
    """Trim old re-readable tool bodies, keeping calls/results and recent text.

    Repeated reads require matching source identity, range, cursor and text;
    path equality alone never establishes that two versions are the same.
    """
    older, tail = split_history(messages, retain_tokens, model, require_older=False)
    old_end = len(older)
    header, groups = _history_groups(messages)
    if not groups:
        return list(messages), {"duplicate_reads": 0, "large_reads": 0}
    calls = {}
    position = len(header)
    for group in groups:
        group_calls = {}
        for call in group[0].get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except (ValueError, TypeError):
                continue
            if isinstance(arguments, dict):
                group_calls[call["id"]] = (function.get("name"), arguments)
        for offset, message in enumerate(group[1:], 1):
            calls[position + offset] = group_calls.get(message.get("tool_call_id"), (None, {}))
        position += len(group)
    output = list(messages)
    latest = {}
    counts = {"duplicate_reads": 0, "large_reads": 0}
    for index in range(len(messages) - 1, len(header) - 1, -1):
        message = messages[index]
        call_id = message.get("tool_call_id")
        name, arguments = calls.get(index, (None, {}))
        if message.get("role") != "tool" or name not in ("read_file", "read_material"):
            continue
        try:
            value = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or value.get("error") or value.get("context_cleanup"):
            continue
        text = value.get("text")
        path = value.get("path")
        if not isinstance(text, str) or not text or not isinstance(path, str):
            continue
        # All fields originate from local readers. JSON serialization also
        # distinguishes absent/explicit root/view and line/character ranges.
        identity = _dump({
            "tool": name, "root": value.get("root", arguments.get("root")),
            "view": value.get("view"), "source_id": value.get("source_id"),
            "path": path, "start_line": value.get("start_line"),
            "line_numbers": value.get("line_numbers"),
            "char_offset": arguments.get("char_offset", 0),
            "returned_chars": len(text),
        })
        later = latest.get(identity)
        latest[identity] = (text, call_id)
        if index >= old_end:
            continue
        replacement = dict(value)
        detail = {"reread": {"name": name, "arguments": arguments},
                  "original_returned_chars": len(text)}
        if later is not None and later[0] == text:
            replacement.pop("text", None)
            detail.update(kind="duplicate_read", duplicate_of_tool_call_id=later[1])
            kind = "duplicate_reads"
        elif len(text) > 8192:
            replacement["text"] = text[:4096] + "\n[Middle omitted from old history; reread the original range if needed.]\n" + text[-1024:]
            detail.update(kind="old_read_excerpt", text_is_excerpt=True,
                          retained_char_ranges=[[0, 4096], [len(text) - 1024, len(text)]])
            kind = "large_reads"
        else:
            continue
        replacement["context_cleanup"] = detail
        encoded = _dump(replacement)
        if len(encoded) >= len(message.get("content", "")):
            continue
        output[index] = dict(message, content=encoded)
        counts[kind] += 1
    return output, counts
