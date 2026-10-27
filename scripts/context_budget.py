"""Offline prompt-token estimates and output-budget planning.

DeepSeek estimates use optional locally installed official tokenizer assets and their chat template.
Other routes use a conservative UTF-8 byte upper bound; neither path calls a
model or accesses the network.
"""

from functools import lru_cache
import json
import math
from pathlib import Path


_TOKENIZER_DIR = Path(__file__).resolve().parent.parent / "assets" / "deepseek_v4_tokenizer"


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=1)
def _load_deepseek_assets():
    """Load the local official tokenizer and chat template once, on demand."""
    tokenizer_json = _TOKENIZER_DIR / "tokenizer.json"
    config_json = _TOKENIZER_DIR / "tokenizer_config.json"
    if not tokenizer_json.is_file() or not config_json.is_file():
        raise RuntimeError("Local DeepSeek tokenizer files are unavailable")
    try:
        from tokenizers import Tokenizer
        from jinja2 import Environment

        config = json.loads(config_json.read_text(encoding="utf-8"))
        chat_template = config.get("chat_template")
        if not isinstance(chat_template, str) or not chat_template:
            raise ValueError("chat_template is missing")
        tokenizer = Tokenizer.from_file(str(tokenizer_json))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        env = Environment(autoescape=False)
        template = env.from_string(chat_template)
    except Exception as exc:
        raise RuntimeError("Could not load the local DeepSeek tokenizer/template") from exc

    bos = config.get("bos_token", "")
    if isinstance(bos, dict):
        bos = bos.get("content", "")
    return tokenizer, template, str(bos or "")


def _native_count(text, tokenizer):
    # Tokenizer.encode defaults to no truncation. add_special_tokens=False is
    # important because the official chat template already emits BOS markers.
    return len(tokenizer.encode(text, add_special_tokens=False).ids)


def _deepseek_estimate(messages, tools, assets=None):
    tokenizer, template, bos_token = assets if assets is not None else _load_deepseek_assets()
    # The runtime sends assistant content as a string for API compatibility.
    # Use a shallow copy so estimating never mutates the caller's messages.
    template_messages = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "assistant" and message.get("content") is None:
            copied = dict(message)
            copied["content"] = ""
            template_messages.append(copied)
        else:
            template_messages.append(message)
    rendered = template.render(
        messages=template_messages,
        bos_token=bos_token,
        add_generation_prompt=True,
    )

    chat_tokens = _native_count(rendered, tokenizer)
    tool_schema_tokens = _native_count(_compact_json(tools), tokenizer) if tools else 0

    # The local chat template only emits tool calls when assistant.content is
    # null. Since runtime requests carry content as a string, count each echoed
    # tool-call object separately to include function names and arguments.
    tool_call_tokens = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if calls:
            tool_call_tokens += _native_count(_compact_json(calls), tokenizer)

    # The official template ignores fields such as reasoning_content. Count
    # them separately so replayed reasoning is not silently omitted.
    reasoning_tokens = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        for field in ("reasoning_content", "reasoning"):
            value = message.get(field)
            if isinstance(value, str) and value:
                reasoning_tokens += _native_count(value, tokenizer)

    # The bundled template currently handles system/user/assistant/tool roles.
    # Preserve a conservative count for any other role it leaves unrendered.
    extra_role_tokens = 0
    known_roles = {"system", "user", "assistant", "tool"}
    for message in messages:
        if isinstance(message, dict) and message.get("role") not in known_roles:
            extra_role_tokens += _native_count(_compact_json(message), tokenizer)

    total = chat_tokens + tool_schema_tokens + tool_call_tokens + reasoning_tokens + extra_role_tokens
    return {
        "input_tokens": total,
        "method": "deepseek_v4_official_tokenizer_template_plus_tool_format_estimate",
        "estimate_note": "本地token估算，不等同于服务端精确usage；2048 token safety margin用于覆盖服务模板差异。",
        "chat_tokens": chat_tokens,
        "tool_tokens": tool_schema_tokens + tool_call_tokens,
        "tool_schema_tokens": tool_schema_tokens,
        "tool_call_tokens": tool_call_tokens,
        "reasoning_tokens": reasoning_tokens,
        "unrendered_role_tokens": extra_role_tokens,
    }


def _utf8_upper_bound(messages, tools):
    # A UTF-8 byte upper bound plus small structural allowances. This is not a
    # claim of tokenizer accuracy for auto or non-DeepSeek models.
    message_bytes = sum(len(_compact_json(message).encode("utf-8")) for message in messages)
    tool_bytes = len(_compact_json(tools).encode("utf-8")) if tools else 0
    overhead = 16 * len(messages) + (16 if tools else 0)
    return {
        "input_tokens": message_bytes + tool_bytes + overhead,
        "method": "utf8_byte_upper_bound_plus_message_overhead",
        "message_bytes": message_bytes,
        "tool_bytes": tool_bytes,
        "structural_overhead": overhead,
    }


def estimate_tokens(messages, tools, model):
    """Estimate input tokens without modifying inputs or using a model/network.

    For DeepSeek Flash routes this uses the bundled v4 tokenizer with no
    truncation. Other routes intentionally report a conservative byte bound.
    """
    if not isinstance(messages, list):
        raise TypeError("messages must be a list")
    if any(not isinstance(message, dict) for message in messages):
        raise TypeError("each message must be an object")
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise TypeError("tools must be a list")
    if any(not isinstance(tool, dict) for tool in tools):
        raise TypeError("each tool must be an object")
    if str(model or "").lower().startswith("deepseek"):
        try:
            assets = _load_deepseek_assets()
        except RuntimeError:
            estimate = _utf8_upper_bound(messages, tools)
            estimate["method"] = "deepseek_utf8_upper_bound_tokenizer_unavailable"
            estimate["count_warning"] = (
                "DeepSeek本地官方tokenizer/template不可用；当前为UTF-8字节上界估算，"
                "不是原生分词结果，也不是服务端精确usage。未联网或安装依赖。"
            )
            return estimate
        return _deepseek_estimate(messages, tools, assets)
    return _utf8_upper_bound(messages, tools)


def estimate_text_tokens(text, model, fallback_bytes_per_token=4):
    """Estimate a material text segment for inline selection.

    DeepSeek uses its bundled tokenizer. Other routes borrow that tokenizer as
    an explicitly approximate proxy. When local tokenizer assets/dependencies
    are unavailable, use the configured UTF-8-bytes-per-token estimate.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    fallback_bytes_per_token = _nonnegative_int(fallback_bytes_per_token, "fallback_bytes_per_token")
    if fallback_bytes_per_token < 1:
        raise ValueError("fallback_bytes_per_token must be positive")

    model_name = str(model or "auto")
    try:
        tokenizer, _template, _bos = _load_deepseek_assets()
    except RuntimeError:
        utf8_bytes = len(text.encode("utf-8"))
        tokens = (utf8_bytes + fallback_bytes_per_token - 1) // fallback_bytes_per_token
        return {
            "tokens": tokens,
            "method": "utf8_bytes_per_4_tokens_fallback" if fallback_bytes_per_token == 4 else "utf8_bytes_per_token_fallback",
            "fallback_bytes_per_token": fallback_bytes_per_token,
            "count_warning": "本地DeepSeek tokenizer/template不可用；当前按UTF-8字节数/每" + str(fallback_bytes_per_token) + "个字节约1 token估算，非精确分词。",
        }

    tokens = _native_count(text, tokenizer)
    if model_name.lower().startswith("deepseek"):
        return {"tokens": tokens, "method": "deepseek_v4_native_text_tokenizer"}
    return {
        "tokens": tokens,
        "method": "deepseek_v4_tokenizer_approximation",
        "count_warning": "材料计数借用DeepSeek v4 tokenizer近似估算" + model_name + "路由，不是该路由的官方精确token计数。",
    }


def estimate_text_prefix(text, token_budget, model, fallback_bytes_per_token=4):
    """Return the longest practical character prefix within a text token budget."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    token_budget = _nonnegative_int(token_budget, "token_budget")
    full = estimate_text_tokens(text, model, fallback_bytes_per_token=fallback_bytes_per_token)
    method = full["method"]
    warning = full.get("count_warning")

    if full["tokens"] <= token_budget:
        prefix = text
        prefix_tokens = full["tokens"]
    elif method in {"utf8_bytes_per_4_tokens_fallback", "utf8_bytes_per_token_fallback"}:
        bytes_per_token = full["fallback_bytes_per_token"]
        low = 0
        high = len(text)
        best = 0
        while low <= high:
            middle = (low + high) // 2
            byte_count = len(text[:middle].encode("utf-8"))
            count = (byte_count + bytes_per_token - 1) // bytes_per_token
            if count <= token_budget:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        prefix = text[:best]
        prefix_bytes = len(prefix.encode("utf-8"))
        prefix_tokens = (prefix_bytes + bytes_per_token - 1) // bytes_per_token
    else:
        tokenizer, _template, _bos = _load_deepseek_assets()
        low = 0
        high = len(text)
        best = 0
        best_tokens = 0
        while low <= high:
            middle = (low + high) // 2
            count = _native_count(text[:middle], tokenizer)
            if count <= token_budget:
                best = middle
                best_tokens = count
                low = middle + 1
            else:
                high = middle - 1
        prefix = text[:best]
        prefix_tokens = _native_count(prefix, tokenizer)
        # Binary search is conservative around token-boundary merges; retain a
        # final exact check to ensure a prefix is never reported over budget.
        while prefix and prefix_tokens > token_budget:
            prefix = prefix[:-1]
            prefix_tokens = _native_count(prefix, tokenizer)
        best_tokens = prefix_tokens
        prefix_tokens = best_tokens

    result = {
        "text": prefix,
        "chars": len(prefix),
        "tokens": prefix_tokens,
        "complete": len(prefix) == len(text),
        "next_char_offset": None if len(prefix) == len(text) else len(prefix),
        "method": method,
    }
    if warning:
        result["count_warning"] = warning
    return result


def _nonnegative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name + " must be a non-negative integer")
    return value


def _split_output_tokens(total, body_cap, thinking_cap):
    """Distribute a total split budget proportionally within route caps."""
    if total <= 0:
        return 0, 0
    if total == 1:
        return 1, 0
    caps = body_cap + thinking_cap
    body = max(1, min(body_cap, total - 1, (total * body_cap) // caps))
    thinking = total - body
    if thinking > thinking_cap:
        thinking = thinking_cap
        body = total - thinking
    if body > body_cap:
        body = body_cap
        thinking = total - body
    return body, thinking


def plan_budget(messages, tools, route, config, output_cap=None):
    """Return an input/output plan; ordinary budget pressure is not an error."""
    route = route or {}
    config = config or {}
    model = route.get("model", "")
    estimate = estimate_tokens(messages, tools, model)

    context_limit = _nonnegative_int(config.get("context_window_tokens", 1_000_000), "context_window_tokens")
    selected_context = route.get("context_window_tokens")
    if selected_context is not None:
        context_limit = min(context_limit, _nonnegative_int(selected_context, "route.context_window_tokens"))
    management = config.get("context_management") or {}
    safety_tokens = _nonnegative_int(management.get("safety_tokens", 2048), "safety_tokens")
    compression_trigger_ratio = management.get("compression_trigger_ratio", 0.90)
    if isinstance(compression_trigger_ratio, bool) or not isinstance(compression_trigger_ratio, (int, float)):
        raise ValueError("compression_trigger_ratio must be a number between 0 and 1")
    if not 0 < compression_trigger_ratio <= 1:
        raise ValueError("compression_trigger_ratio must be greater than 0 and at most 1")
    compression_trigger_tokens = math.floor(context_limit * compression_trigger_ratio)
    route_input_limit = route.get("max_input_tokens")
    if route_input_limit is not None:
        route_input_limit = _nonnegative_int(route_input_limit, "route.max_input_tokens")
        compression_trigger_tokens = min(compression_trigger_tokens, route_input_limit)

    body_cap = route.get("max_tokens")
    thinking_cap = route.get("thinking_budget")
    split_output_budget = (
        route.get("output_limit_parameter") == "max_tokens"
        and body_cap is not None
        and thinking_cap is not None
        and _nonnegative_int(body_cap, "route.max_tokens") > 0
        and _nonnegative_int(thinking_cap, "route.thinking_budget") > 0
    )
    if split_output_budget:
        body_cap = _nonnegative_int(body_cap, "route.max_tokens")
        thinking_cap = _nonnegative_int(thinking_cap, "route.thinking_budget")
        route_output_cap = body_cap + thinking_cap
    else:
        body_cap = None
        thinking_cap = None
        route_output_cap = None

    if output_cap is not None:
        requested_output_tokens = _nonnegative_int(output_cap, "output_cap")
    elif split_output_budget:
        requested_output_tokens = route_output_cap
    elif route.get("max_tokens") is not None:
        requested_output_tokens = _nonnegative_int(route["max_tokens"], "route.max_tokens")
    else:
        requested_output_tokens = _nonnegative_int(
            management.get("auto_output_reserve_tokens", 393_216),
            "auto_output_reserve_tokens",
        )

    remaining = compression_trigger_tokens - estimate["input_tokens"] - safety_tokens
    # Reserve the complete configured generation budget before a review call.
    # An input-only trigger could leave just a few thousand reasoning tokens;
    # compact first instead of sending such a reduced request to the provider.
    generation_reserve = min(requested_output_tokens, route_output_cap) if route_output_cap is not None else requested_output_tokens
    output_budget_exceeds_context = generation_reserve >= compression_trigger_tokens - safety_tokens
    needs_compaction = remaining <= generation_reserve or estimate["input_tokens"] >= compression_trigger_tokens - safety_tokens
    if split_output_budget and generation_reserve < 2:
        needs_compaction = True
    effective_output_tokens = 0 if needs_compaction else generation_reserve

    if split_output_budget:
        effective_body_tokens, effective_thinking_tokens = _split_output_tokens(
            effective_output_tokens, body_cap, thinking_cap
        )
    else:
        effective_body_tokens = effective_output_tokens
        effective_thinking_tokens = None

    result = {
        "input_tokens": estimate["input_tokens"],
        "method": estimate["method"],
        "context_limit": context_limit,
        "safety_tokens": safety_tokens,
        "compression_trigger_tokens": compression_trigger_tokens,
        "compression_trigger_ratio": compression_trigger_ratio,
        "max_input_tokens": route_input_limit,
        "remaining_tokens": remaining,
        "requested_output_tokens": requested_output_tokens,
        "effective_output_tokens": effective_output_tokens,
        "output_limited_by_context": effective_output_tokens < requested_output_tokens,
        "needs_compaction": needs_compaction,
        "compaction_reason": ("output_reservation" if remaining <= generation_reserve else "input_trigger") if needs_compaction else None,
        "output_budget_exceeds_context": output_budget_exceeds_context,
        "generation_reserve_tokens": generation_reserve,
        "split_output_budget": split_output_budget,
        "requested_body_tokens": body_cap,
        "effective_body_tokens": effective_body_tokens,
        "requested_thinking_tokens": thinking_cap,
        "effective_thinking_tokens": effective_thinking_tokens,
        "effective_max_tokens": effective_body_tokens,
        "effective_thinking_budget": effective_thinking_tokens,
    }
    if estimate.get("count_warning"):
        result["count_warning"] = estimate["count_warning"]
    return result
