"""In-memory read-only material index for review context tools.

Only snapshots supplied in packet['files'] and the optional virtual @diff are
visible. Paths are identifiers; this module never opens them or accesses a
network, shell, or external tool.
"""

import json


_EXCLUDED_METADATA_KEYS = {
    "diff", "files", "findings", "review", "reviewer", "review_report",
    "main_review", "main_gpt_review", "main_gpt_conclusion", "gpt_conclusion",
    "assistant_conclusion", "conclusion", "verdict", "model_verdict", "report",
    "result", "results", "test_result", "test_results", "actual_result",
    "failure", "failures", "failure_log", "failed_logs",
    "failure_details", "test_logs", "test_output", "failed_test_output", "traceback",
    "logs", "stdout", "stderr", "output", "response", "error",
}
_TASK_FIELDS = (
    "task", "requirements", "acceptance_criteria", "scope", "tests",
    "test_plan", "test_notes", "tests_description", "test_commands",
    "testing_notes", "validation_notes", "test_results",
)
_TEST_METADATA_FIELDS = ("description", "plan", "commands", "command", "purpose", "scope", "expected")
_MAX_TASK_CONTEXT_CHARS = 8_000
_MAX_METADATA_VALUE_CHARS = 2_500
_METADATA_PREVIEW_CHARS = 1_200


def _content_string(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2)


def _line_count(content):
    return len(content.splitlines(keepends=True))


def _normalized_key(value):
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _metadata_value(value, remaining, allowed_keys=None):
    """Copy bounded metadata while excluding logs and review conclusions."""
    if remaining[0] <= 0:
        return None
    if isinstance(value, str):
        text = value[: min(_MAX_METADATA_VALUE_CHARS, remaining[0])]
        remaining[0] -= len(text)
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        result = []
        for item in value[:20]:
            copied = _metadata_value(item, remaining, allowed_keys=allowed_keys)
            if copied is not None:
                result.append(copied)
            if remaining[0] <= 0:
                break
        return result
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _EXCLUDED_METADATA_KEYS:
                continue
            if allowed_keys is not None and normalized not in allowed_keys:
                continue
            copied = _metadata_value(item, remaining, allowed_keys=allowed_keys)
            if copied is not None:
                result[str(key)] = copied
            if remaining[0] <= 0:
                break
        return result
    return None


class MaterialStore:
    """Bounded in-memory reader for packet snapshots; never resolves paths."""

    def __init__(self, packet, max_chars=24_000, max_lines=250):
        if not isinstance(packet, dict):
            raise TypeError("packet must be an object")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            raise ValueError("max_chars must be a positive integer")
        if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1:
            raise ValueError("max_lines must be a positive integer")
        self._packet = packet
        self.max_chars = max_chars
        self.max_lines = max_lines
        self._materials = []
        self._metadata_paths = {}
        self._build_materials()

    def _unique_alias(self, preferred):
        alias = preferred
        suffix = 2
        while any(material["path"] == alias for material in self._materials):
            alias = preferred + "#snapshot-" + str(suffix)
            suffix += 1
        return alias

    def _add(self, preferred_path, content, kind, source_path=None, note=None, source_field=None):
        # Equality-based deduplication preserves identical snapshots without
        # checksums, fingerprints, or content-derived identifiers.
        for material in self._materials:
            if (source_path is not None and material["source_path"] == source_path
                    and material["content"] == content and material.get("source_field") == source_field):
                return material["path"]

        alias = self._unique_alias(preferred_path)
        actual_note = note
        if alias != preferred_path:
            collision_note = "同名路径内容冲突，作为独立快照保留。"
            actual_note = (actual_note + " " if actual_note else "") + collision_note
        self._materials.append({
            "path": alias,
            "source_path": source_path,
            "kind": kind,
            "content": content,
            "lines": _line_count(content),
            "chars": len(content),
            "note": actual_note,
            "source_field": source_field,
        })
        return alias

    def _build_materials(self):
        snapshots = self._packet.get("files", [])
        if isinstance(snapshots, list):
            for index, item in enumerate(snapshots, start=1):
                if not isinstance(item, dict):
                    continue
                raw_path = item.get("path")
                source_path = str(raw_path) if raw_path is not None and str(raw_path) else None
                if source_path:
                    preferred = source_path
                    note = None
                else:
                    preferred = "@file-" + str(index)
                    note = "原始快照没有路径字段。"
                content = _content_string(item.get("content"))

                # When an earlier snapshot has the same path and different
                # contents, keep this one under a clear alias.
                prior_same_path = [m for m in self._materials if source_path is not None and m["source_path"] == source_path]
                if prior_same_path and all(m["content"] != content for m in prior_same_path):
                    note = (note + " " if note else "") + "与同路径的其他快照内容不同。"
                self._add(preferred, content, "file", source_path=source_path, note=note)

        diff = self._packet.get("diff")
        if diff is not None:
            diff_text = _content_string(diff)
            if diff_text:
                self._add("@diff", diff_text, "diff", source_path="\x00virtual-diff", source_field="diff")

        for field in _TASK_FIELDS:
            if field not in self._packet or self._packet.get(field) is None:
                continue
            value = self._packet[field]
            serialized = _content_string(value)
            if field == "test_results" or len(serialized) > _METADATA_PREVIEW_CHARS:
                self._ensure_metadata_material(field, value)

    def _ensure_metadata_material(self, field, value):
        if field in self._metadata_paths:
            return self._metadata_paths[field]
        content = _content_string(value)
        alias = self._add(
            "@" + field,
            content,
            "metadata",
            source_path="\x00metadata:" + field,
            note="原始packet字段：" + field,
            source_field=field,
        )
        self._metadata_paths[field] = alias
        return alias

    def index(self):
        """Return metadata only; material content is available through read."""
        return [
            {key: material[key] for key in ("path", "kind", "source_field", "lines", "chars", "note") if material.get(key) is not None}
            for material in self._materials
        ]

    def task_context(self):
        """Return bounded task metadata with explicit pointers for omitted text."""
        result = {}
        remaining = [_MAX_TASK_CONTEXT_CHARS]
        for key in _TASK_FIELDS:
            if key not in self._packet:
                continue
            value = self._packet.get(key)
            if value is None:
                continue

            if key == "test_results":
                path = self._ensure_metadata_material(key, value)
                result[key] = {"material_path": path, "has_more": bool(_content_string(value))}
                continue

            allowed = None
            if key == "tests" and isinstance(value, (dict, list)):
                allowed = {_normalized_key(item) for item in _TEST_METADATA_FIELDS}

            if isinstance(value, str):
                preview_length = min(len(value), _METADATA_PREVIEW_CHARS, remaining[0])
                if preview_length == len(value):
                    result[key] = value
                    remaining[0] -= preview_length
                else:
                    path = self._ensure_metadata_material(key, value)
                    result[key] = {
                        "preview": value[:preview_length],
                        "has_more": True,
                        "material_path": path,
                    }
                    remaining[0] -= preview_length
                continue

            local_remaining = [min(remaining[0], _METADATA_PREVIEW_CHARS)]
            safe = _metadata_value(value, local_remaining, allowed_keys=allowed)
            was_omitted = safe != value
            consumed = min(remaining[0], _METADATA_PREVIEW_CHARS) - local_remaining[0]
            if not was_omitted:
                result[key] = safe
                remaining[0] -= consumed
                continue

            path = self._ensure_metadata_material(key, value)
            result[key] = {
                "preview": safe,
                "has_more": True,
                "material_path": path,
            }
            remaining[0] -= consumed
        return result

    @staticmethod
    def _cursor_for_offset(content, char_offset):
        """Convert an exact Python-character offset into read_material cursors."""
        lines = content.splitlines(keepends=True)
        consumed = 0
        for index, line in enumerate(lines):
            line_end = consumed + len(line)
            if char_offset < line_end:
                return index + 1, char_offset - consumed
            consumed = line_end
            if char_offset == consumed:
                return index + 2, 0
        return len(lines) + 1, 0

    @staticmethod
    def _count_result(value):
        if isinstance(value, int) and not isinstance(value, bool):
            result = {"tokens": value, "method": "injected_counter"}
        elif isinstance(value, dict):
            result = dict(value)
            if "tokens" not in result and "input_tokens" in result:
                result["tokens"] = result["input_tokens"]
        else:
            raise TypeError("counter must return an integer or token-count object")
        tokens = result.get("tokens")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("counter token count must be a non-negative integer")
        result.setdefault("method", "injected_counter")
        return result

    def inline_materials(self, token_budget=100_000, model="auto", counter=None, fallback_bytes_per_token=4):
        """Inline priority material text up to a budget and return exact read cursors.

        A test counter has the signature `counter(text, model)` and returns an
        integer or a mapping with `tokens` and optional method/warning fields.
        """
        if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 0:
            raise ValueError("token_budget must be a non-negative integer")
        if isinstance(fallback_bytes_per_token, bool) or not isinstance(fallback_bytes_per_token, int) or fallback_bytes_per_token < 1:
            raise ValueError("fallback_bytes_per_token must be a positive integer")

        try:
            from . import context_budget
        except ImportError:
            import context_budget

        if counter is None:
            def count_text(text):
                return self._count_result(context_budget.estimate_text_tokens(
                    text, model, fallback_bytes_per_token=fallback_bytes_per_token
                ))

            def prefix_text(text, budget):
                return context_budget.estimate_text_prefix(
                    text, budget, model, fallback_bytes_per_token=fallback_bytes_per_token
                )
        else:
            def count_text(text):
                return self._count_result(counter(text, model))

            def prefix_text(text, budget):
                low, high, best = 0, len(text), 0
                while low <= high:
                    middle = (low + high) // 2
                    current = count_text(text[:middle])
                    if current["tokens"] <= budget:
                        best = middle
                        low = middle + 1
                    else:
                        high = middle - 1
                snippet = text[:best]
                counted = count_text(snippet)
                while snippet and counted["tokens"] > budget:
                    snippet = snippet[:-1]
                    counted = count_text(snippet)
                result = {
                    "text": snippet,
                    "chars": len(snippet),
                    "tokens": counted["tokens"],
                    "complete": len(snippet) == len(text),
                    "next_char_offset": None if len(snippet) == len(text) else len(snippet),
                    "method": counted["method"],
                }
                if counted.get("count_warning"):
                    result["count_warning"] = counted["count_warning"]
                return result

        task_context = self.task_context()
        for material in self._materials:
            if material["kind"] == "metadata" and material.get("source_field"):
                task_context.pop(material["source_field"], None)

        context_text = json.dumps(task_context, ensure_ascii=False, separators=(",", ":"))
        context_count = count_text(context_text)
        used_tokens = context_count["tokens"]
        if used_tokens > token_budget and task_context:
            for field in _TASK_FIELDS:
                if field in task_context:
                    self._ensure_metadata_material(field, self._packet.get(field))
                    task_context.pop(field, None)
            context_text = json.dumps(task_context, ensure_ascii=False, separators=(",", ":"))
            context_count = count_text(context_text)
            used_tokens = context_count["tokens"]

        priority_fields = {field: index for index, field in enumerate(_TASK_FIELDS)}
        ordered = sorted(
            self._materials,
            key=lambda material: (
                0 if material["kind"] == "metadata" else 1 if material["kind"] == "diff" else 2,
                priority_fields.get(material.get("source_field"), len(priority_fields)),
            ),
        )
        remaining_budget = max(0, token_budget - used_tokens)
        inline_items = []
        delivered_ranges = []
        remaining_materials = []
        methods = []
        warnings = []
        inline_chars = 0
        inline_lines = 0
        material_tokens = 0

        for material in ordered:
            content = material["content"]
            full_count = count_text(content)
            method = full_count["method"]
            if method not in methods:
                methods.append(method)
            if full_count.get("count_warning") and full_count["count_warning"] not in warnings:
                warnings.append(full_count["count_warning"])

            if full_count["tokens"] <= remaining_budget:
                snippet = content
                snippet_tokens = full_count["tokens"]
                complete = True
            else:
                prefix = prefix_text(content, remaining_budget)
                snippet = prefix["text"]
                snippet_tokens = prefix["tokens"]
                complete = prefix["complete"]
                if prefix.get("method") and prefix["method"] not in methods:
                    methods.append(prefix["method"])
                if prefix.get("count_warning") and prefix["count_warning"] not in warnings:
                    warnings.append(prefix["count_warning"])

            if snippet or not content:
                end_line, end_char = self._cursor_for_offset(content, len(snippet))
                segment_lines = _line_count(snippet)
                item = {
                    "path": material["path"],
                    "kind": material["kind"],
                    "start_line": 1,
                    "start_char_offset": 0,
                    "end_line": end_line,
                    "end_char_offset": end_char,
                    "end_is_exclusive": True,
                    "chars": len(snippet),
                    "lines": segment_lines,
                    "estimated_tokens": snippet_tokens,
                    "method": prefix.get("method", method) if not complete else method,
                    "text": snippet,
                }
                if material.get("source_field"):
                    item["source_field"] = material["source_field"]
                inline_items.append(item)
                delivered_ranges.append({key: item[key] for key in (
                    "path", "start_line", "start_char_offset", "end_line", "end_char_offset", "end_is_exclusive", "chars", "lines"
                )})
                inline_chars += len(snippet)
                inline_lines += segment_lines
                material_tokens += snippet_tokens
                used_tokens += snippet_tokens
                remaining_budget = max(0, token_budget - used_tokens)
            if not complete:
                cursor_line, cursor_char = self._cursor_for_offset(content, len(snippet))
                rest = {
                    "path": material["path"],
                    "kind": material["kind"],
                    "cursor": {"start_line": cursor_line, "char_offset": cursor_char},
                    "remaining_chars": len(content) - len(snippet),
                }
                if material.get("source_field"):
                    rest["source_field"] = material["source_field"]
                remaining_materials.append(rest)

        return {
            "task_context": task_context,
            "materials": inline_items,
            "delivered_ranges": delivered_ranges,
            "inline_chars": inline_chars,
            "inline_lines": inline_lines,
            "task_context_chars": len(context_text),
            "task_context_estimated_tokens": context_count["tokens"],
            "material_estimated_tokens": material_tokens,
            "estimated_tokens": context_count["tokens"] + material_tokens,
            "token_budget": token_budget,
            "method": methods[0] if len(methods) == 1 else "mixed_material_estimators",
            "methods": methods,
            "fully_inlined": not remaining_materials,
            "needs_tools": bool(remaining_materials),
            "remaining_materials": remaining_materials,
            **({"count_warning": " ".join(warnings)} if warnings else {}),
        }

    def tools(self):
        """OpenAI function-tool schemas for the bounded in-memory reader."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_materials",
                    "description": "列出材料路径及类型、行数和字符数，不返回材料正文。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "offset": {"type": "integer", "minimum": 0, "default": 0},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_material",
                    "description": "从材料索引读取一段快照内容；使用返回的游标继续分页。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1, "default": 1},
                            "line_count": {"type": "integer", "minimum": 1, "default": self.max_lines},
                            "char_offset": {"type": "integer", "minimum": 0, "default": 0},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_material",
                    "description": "在已索引材料中按字面文本搜索，并返回有限的位置片段。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "minLength": 1, "maxLength": 160},
                            "path": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    @staticmethod
    def _error(message):
        return {"error": message}

    @staticmethod
    def _arguments_object(arguments):
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return None
        return arguments if isinstance(arguments, dict) else None

    @staticmethod
    def _valid_int(value, minimum=0):
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum

    def _find(self, path):
        if not isinstance(path, str):
            return None
        for material in self._materials:
            if material["path"] == path:
                return material
        return None

    def execute(self, name, arguments):
        """Execute one allowlisted tool against this instance's memory only."""
        args = self._arguments_object(arguments)
        if args is None:
            return self._error("arguments must be a JSON object")

        if name == "list_materials":
            if set(args) - {"offset", "limit"}:
                return self._error("unknown argument")
            offset = args.get("offset", 0)
            limit = args.get("limit", 20)
            if not self._valid_int(offset) or not self._valid_int(limit, 1):
                return self._error("offset and limit must be non-negative/positive integers")
            limit = min(limit, 50)
            items = self.index()[offset:offset + limit]
            return {
                "items": items,
                "offset": offset,
                "total": len(self._materials),
                "has_more": offset + len(items) < len(self._materials),
            }

        if name == "read_material":
            if set(args) - {"path", "start_line", "line_count", "char_offset"}:
                return self._error("unknown argument")
            material = self._find(args.get("path"))
            if material is None:
                return self._error("path is not in the material index")
            start_line = args.get("start_line", 1)
            requested_lines = args.get("line_count", self.max_lines)
            char_offset = args.get("char_offset", 0)
            if not self._valid_int(start_line, 1):
                return self._error("start_line must be a positive integer")
            if not self._valid_int(requested_lines, 1):
                return self._error("line_count must be a positive integer")
            if not self._valid_int(char_offset):
                return self._error("char_offset must be a non-negative integer")

            lines = material["content"].splitlines(keepends=True)
            total_lines = len(lines)
            if start_line > total_lines + 1:
                return self._error("start_line is outside this material")
            index = start_line - 1
            if index == total_lines:
                if char_offset != 0:
                    return self._error("char_offset is outside this material")
                return {
                    "path": material["path"], "start_line": start_line,
                    "line_numbers": [], "text": "", "total_lines": total_lines,
                    "total_chars": material["chars"], "has_more": False,
                    "next_start_line": None, "next_char_offset": None,
                }
            if char_offset > len(lines[index]):
                return self._error("char_offset is outside the requested line")
            if char_offset == len(lines[index]):
                index += 1
                char_offset = 0
                start_line += 1
                if index == total_lines:
                    return {
                        "path": material["path"], "start_line": start_line,
                        "line_numbers": [], "text": "", "total_lines": total_lines,
                        "total_chars": material["chars"], "has_more": False,
                        "next_start_line": None, "next_char_offset": None,
                    }

            line_limit = min(requested_lines, self.max_lines)
            cursor_index = index
            cursor_offset = char_offset
            parts = []
            line_numbers = []
            chars_left = self.max_chars
            included_lines = 0
            has_more = False

            while cursor_index < total_lines and included_lines < line_limit and chars_left > 0:
                current = lines[cursor_index]
                part = current[cursor_offset:cursor_offset + chars_left]
                parts.append(part)
                line_numbers.append(cursor_index + 1)
                chars_left -= len(part)
                included_lines += 1

                consumed_to = cursor_offset + len(part)
                if consumed_to < len(current):
                    has_more = True
                    cursor_offset = consumed_to
                    break
                cursor_index += 1
                cursor_offset = 0

            if not has_more and cursor_index < total_lines:
                has_more = True
            if has_more:
                next_start_line = cursor_index + 1
                next_char_offset = cursor_offset
            else:
                next_start_line = None
                next_char_offset = None

            return {
                "path": material["path"],
                "start_line": line_numbers[0] if line_numbers else start_line,
                "line_numbers": line_numbers,
                "text": "".join(parts),
                "total_lines": total_lines,
                "total_chars": material["chars"],
                "has_more": has_more,
                "next_start_line": next_start_line,
                "next_char_offset": next_char_offset,
            }

        if name == "search_material":
            if set(args) - {"query", "path", "limit"}:
                return self._error("unknown argument")
            query = args.get("query")
            if not isinstance(query, str) or not query:
                return self._error("query must be a non-empty string")
            if len(query) > 160:
                return self._error("query is too long")
            path = args.get("path")
            if path is not None and self._find(path) is None:
                return self._error("path is not in the material index")
            limit = args.get("limit", 20)
            if not self._valid_int(limit, 1):
                return self._error("limit must be a positive integer")
            limit = min(limit, 20)

            matches = []
            has_more = False
            materials = [self._find(path)] if path is not None else self._materials
            for material in materials:
                content = material["content"]
                start = 0
                while True:
                    position = content.find(query, start)
                    if position < 0:
                        break
                    line_start = content.rfind("\n", 0, position) + 1
                    line_number = content.count("\n", 0, position) + 1
                    snippet_start = max(0, position - 72)
                    snippet_end = min(len(content), position + len(query) + 72)
                    match = {
                        "path": material["path"],
                        "line": line_number,
                        "char_offset": position - line_start,
                        "snippet": content[snippet_start:snippet_end],
                    }
                    if len(matches) >= limit:
                        has_more = True
                        return {"matches": matches, "limit": limit, "has_more": has_more}
                    matches.append(match)
                    start = position + max(1, len(query))
            return {"matches": matches, "limit": limit, "has_more": has_more}

        return self._error("unknown tool")
