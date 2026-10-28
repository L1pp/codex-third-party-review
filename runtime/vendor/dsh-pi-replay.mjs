/*
Extracted from @deepseek-ai/dsh-llm-pi-ai 0.2.1-rc.2, package/lib/index.js.
https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/llm/llm-pi-ai

MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
import { LlmError, contentHasImage } from "@deepseek-ai/dsh-llm";
import { brandString } from "@deepseek-ai/dsh-brand";
//#region lib/types/replay.js
/**
* Durable pi-ai replay metadata and assistant-history reconstruction.
*
* Harness content remains the durable source for text and tool calls. This
* module stores only the provider-native metadata needed to reconstruct a
* pi-ai assistant message on a later request.
*
* @module dsh-llm-pi-ai/replay
*/
/** Parse tool-call argument JSON; tolerate model malformations with {}. */
function parseArguments(raw) {
	try {
		const parsed = JSON.parse(raw);
		if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) return parsed;
	} catch {}
	return {};
}
/** Construct the zero usage value required by historical pi-ai messages. */
function emptyPiUsage() {
	return {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		cost: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			total: 0
		}
	};
}
/**
* Project a successful pi-ai response into the minimal durable replay state.
* The per-block half is index-aligned with the streamed blocks (pi-ai content
* order), so `BlockAssembler` prunes an entry with its block whenever assembly
* removes one.
* @param message - completed native pi-ai assistant response.
* @param requestedModel - request identity stored in the assistant source; defaults to the native model.
* @returns the versioned lossless-JSON replay projection.
*/
function toPiReplayState(message, requestedModel = message.model) {
	return {
		response: {
			kind: "pi-ai",
			version: 2,
			api: message.api,
			provider: message.provider,
			model: requestedModel,
			...message.responseModel === void 0 ? {} : { responseModel: message.responseModel },
			...message.responseId === void 0 ? {} : { responseId: message.responseId },
			...message.providerThinkingLevel === void 0 ? {} : { providerThinkingLevel: message.providerThinkingLevel },
			stopReason: message.stopReason
		},
		blocks: message.content.map((block) => {
			switch (block.type) {
				case "text": return {
					type: "text",
					...block.textSignature === void 0 ? {} : { textSignature: block.textSignature }
				};
				case "thinking": return {
					type: "reasoning",
					...block.thinkingSignature === void 0 ? {} : { thinkingSignature: block.thinkingSignature },
					...block.redacted === void 0 ? {} : { redacted: block.redacted }
				};
				case "toolCall": return {
					type: "tool-call",
					...block.thoughtSignature === void 0 ? {} : { thoughtSignature: block.thoughtSignature },
					...block.namespace === void 0 ? {} : { namespace: block.namespace }
				};
			}
		})
	};
}
function invalidReplay(message) {
	throw new LlmError(`invalid pi-ai replay state: ${message}`, "INVALID_REPLAY_STATE");
}
/** Validate the durable adapter-private envelope before it reaches pi-ai. */
function readReplayState(value) {
	if (typeof value !== "object" || value === null || Array.isArray(value)) return invalidReplay("expected a replay envelope");
	const envelope = value;
	const rawResponse = envelope["response"];
	if (typeof rawResponse !== "object" || rawResponse === null || Array.isArray(rawResponse)) return invalidReplay("expected a response object");
	const response = rawResponse;
	if (response["kind"] !== "pi-ai") return invalidReplay("unknown state kind");
	if (response["version"] !== 2) return invalidReplay(`unsupported version ${String(response["version"])}`);
	for (const key of [
		"api",
		"provider",
		"model"
	]) if (typeof response[key] !== "string" || response[key].length === 0) return invalidReplay(`${key} must be a non-empty string`);
	if (![
		"stop",
		"length",
		"toolUse",
		"error",
		"aborted"
	].includes(String(response["stopReason"]))) return invalidReplay("unknown stopReason");
	if (response["responseModel"] !== void 0 && typeof response["responseModel"] !== "string") return invalidReplay("responseModel must be a string");
	if (response["responseId"] !== void 0 && typeof response["responseId"] !== "string") return invalidReplay("responseId must be a string");
	if (response["providerThinkingLevel"] !== void 0 && typeof response["providerThinkingLevel"] !== "string") return invalidReplay("providerThinkingLevel must be a string");
	const blocks = envelope["blocks"];
	if (!Array.isArray(blocks)) return invalidReplay("blocks must be an array");
	for (const [index, value] of blocks.entries()) {
		if (typeof value !== "object" || value === null || Array.isArray(value)) return invalidReplay(`block ${index} must be an object`);
		const block = value;
		if (![
			"text",
			"reasoning",
			"tool-call"
		].includes(String(block["type"]))) return invalidReplay(`block ${index} has an unknown type`);
		for (const signature of [
			"textSignature",
			"thinkingSignature",
			"thoughtSignature",
			"namespace"
		]) if (block[signature] !== void 0 && typeof block[signature] !== "string") return invalidReplay(`block ${index} ${signature} must be a string`);
		if (block["redacted"] !== void 0 && typeof block["redacted"] !== "boolean") return invalidReplay(`block ${index} redacted must be boolean`);
	}
	return {
		response,
		blocks
	};
}
/** Convert provider-neutral blocks without trusting them as same-model replay. */
function foreignAssistant(message) {
	const source = message.source;
	const content = [];
	for (const block of message.content) switch (block.type) {
		case "text":
			content.push({
				type: "text",
				text: block.text
			});
			break;
		case "reasoning":
			content.push({
				type: "thinking",
				thinking: block.text
			});
			break;
		case "tool-call":
			content.push({
				type: "toolCall",
				id: block.id,
				name: block.name,
				arguments: parseArguments(block.arguments)
			});
			break;
		case "image": throw new LlmError("pi-ai chat history cannot represent structured assistant image output", "UNSUPPORTED_CONTENT");
		default: break;
	}
	return {
		role: "assistant",
		content,
		api: "dsh-foreign",
		provider: source.provider,
		model: source.model,
		usage: emptyPiUsage(),
		stopReason: content.some((piece) => piece.type === "toolCall") ? "toolUse" : "stop",
		timestamp: 0
	};
}
/** Recombine durable Harness content with validated pi-ai replay metadata. */
function replayedAssistant(message, source, rawState) {
	const state = readReplayState(rawState);
	if (state.response.provider !== source.provider) return invalidReplay("provider does not match assistant source");
	if (state.response.model !== source.model) return invalidReplay("model does not match assistant source");
	if (state.blocks.length !== message.content.length) return invalidReplay("block count does not match assistant content");
	return {
		role: "assistant",
		content: message.content.map((block, index) => {
			const replay = state.blocks[index];
			if (replay === void 0 || replay.type !== block.type) return invalidReplay(`block ${index} does not match assistant content`);
			switch (block.type) {
				case "text": return {
					type: "text",
					text: block.text,
					...replay.type === "text" && replay.textSignature !== void 0 ? { textSignature: replay.textSignature } : {}
				};
				case "reasoning": return {
					type: "thinking",
					thinking: block.text,
					...replay.type === "reasoning" && replay.thinkingSignature !== void 0 ? { thinkingSignature: replay.thinkingSignature } : {},
					...replay.type === "reasoning" && replay.redacted !== void 0 ? { redacted: replay.redacted } : {}
				};
				case "tool-call": return {
					type: "toolCall",
					id: block.id,
					name: block.name,
					arguments: parseArguments(block.arguments),
					...replay.type === "tool-call" && replay.thoughtSignature !== void 0 ? { thoughtSignature: replay.thoughtSignature } : {},
					...replay.type === "tool-call" && replay.namespace !== void 0 ? { namespace: replay.namespace } : {}
				};
				/* v8 ignore next -- readReplayState rejects unknown replay tags, so an equal plugin-added Harness tag cannot reach this switch */
				default: return invalidReplay(`block ${index} has an unsupported Harness type`);
			}
		}),
		api: state.response.api,
		provider: state.response.provider,
		model: state.response.model,
		...state.response.responseModel === void 0 ? {} : { responseModel: state.response.responseModel },
		...state.response.responseId === void 0 ? {} : { responseId: state.response.responseId },
		...state.response.providerThinkingLevel === void 0 ? {} : { providerThinkingLevel: state.response.providerThinkingLevel },
		usage: emptyPiUsage(),
		stopReason: state.response.stopReason,
		timestamp: 0
	};
}
/**
* Convert one durable Harness assistant message into pi-ai history.
*
* Durable content is the authoritative record; replay metadata only restores
* native fidelity (ids, signatures). A replay state this build cannot use —
* another adapter's kind, another version, a malformed value, or metadata that
* no longer matches the content — therefore degrades the one message to
* provider-neutral history instead of failing the request.
* @param message - model-produced assistant content with provider, model, and optional adapter-owned replay metadata.
* @param onDegrade - called with the diagnostic reason when an unusable replay
*   state falls back to provider-neutral conversion.
* @returns a native pi-ai assistant message reconstructed from durable content.
*/
function toPiAssistant(message, onDegrade) {
	const source = message.source;
	if (source.replayState === void 0) return foreignAssistant(message);
	try {
		return replayedAssistant(message, source, source.replayState);
	} catch (error) {
		/* v8 ignore next -- replayedAssistant throws only INVALID_REPLAY_STATE LlmErrors; the
		guard keeps a future non-replay failure loud instead of silently degrading it */
		if (!(error instanceof LlmError) || error.code !== "INVALID_REPLAY_STATE") throw error;
		onDegrade?.(error.message);
		return foreignAssistant(message);
	}
}
function flattenText(message) {
	return message.content.filter((block) => block.type === "text").map((block) => block.text).join("");
}
/** Recover the pi-ai toolResult message for one harness tool-role message. */
function toolResultOf(message, toolNames, content) {
	return {
		role: "toolResult",
		toolCallId: message.toolCallId,
		toolName: toolNames.get(message.toolCallId) ?? "unknown",
		content: typeof content === "string" ? [{
			type: "text",
			text: content || "(no output)"
		}] : content,
		isError: message.isError ?? false,
		timestamp: 0
	};
}
/** Reject misplaced tool-change blocks and unsupported image roles before replay or image offloading. */
function assertSupportedHistory(messages) {
	for (const message of messages) {
		if (message.role !== "developer" && message.content.some((block) => block.type === "tool-addition" || block.type === "tool-removal")) throw new LlmError("Tool-change blocks require developer role", "UNSUPPORTED_CONTENT");
		if (message.role !== "user" && message.role !== "tool" && contentHasImage(message.content)) throw new LlmError(`pi-ai cannot represent an image in an in-history ${message.role} message`, "UNSUPPORTED_CONTENT");
		if (message.role === "system" && message.content.some((block) => block.type !== "text")) throw new LlmError("pi-ai cannot represent non-text system messages", "UNSUPPORTED_CONTENT");
	}
}
function piTool(tool) {
	return {
		name: tool.name,
		description: tool.description,
		parameters: tool.parameters
	};
}
function toolsOf(options) {
	return options.tools?.filter((tool) => !tool.deferLoading).map(piTool);
}
/** Combine request-level text and leading system history into the initial prompt. */
function splitSystemPrompt(options) {
	const text = [options.system];
	let end = 0;
	for (const message of options.messages) {
		if (message.role !== "system") break;
		text.push(flattenText(message));
		end++;
	}
	return {
		systemPrompt: text.filter(Boolean).join("\n\n") || void 0,
		messages: options.messages.slice(end)
	};
}
/** Assemble the request-level pi-ai context envelope shared by both conversion paths. */
function piContext(systemPrompt, options, messages) {
	const tools = toolsOf(options);
	return {
		...systemPrompt !== void 0 ? { systemPrompt } : {},
		messages,
		...tools !== void 0 && tools.length > 0 ? { tools } : {}
	};
}
/** Keep one developer event in one system update, resolving its projected tool declarations. */
function appendDeveloper(message, options, messages) {
	const content = [];
	const toolsAdded = [];
	const toolsRemoved = [];
	for (const block of message.content) switch (block.type) {
		case "text":
			if (block.text.length > 0) content.push({
				type: "text",
				text: block.text
			});
			break;
		case "tool-addition": {
			const tool = options.tools?.find((tool) => tool.name === block.toolName);
			if (tool === void 0) throw new LlmError(`pi-ai tool update references undeclared tool "${block.toolName}"`, "INVALID_REQUEST");
			toolsAdded.push(piTool(tool));
			break;
		}
		case "tool-removal":
			toolsRemoved.push({ name: block.toolName });
			break;
		default: throw new LlmError(`pi-ai cannot represent developer content ${block.type}`, "UNSUPPORTED_CONTENT");
	}
	if (content.length === 0 && toolsAdded.length === 0 && toolsRemoved.length === 0) return;
	messages.push({
		role: "system",
		content,
		timestamp: 0,
		...toolsAdded.length === 0 ? {} : { toolsAdded },
		...toolsRemoved.length === 0 ? {} : { toolsRemoved }
	});
}
function appendAssistant(message, messages, toolNames, onReplayDegrade) {
	const assistant = toPiAssistant(message, onReplayDegrade);
	for (const block of assistant.content) if (block.type === "toolCall") toolNames.set(brandString(block.id), block.name);
	messages.push(assistant);
}
/** Append shared system, developer, and assistant roles; true when consumed. */
function appendSharedHistory(message, options, messages, toolNames, onReplayDegrade) {
	if (message.role === "developer") {
		appendDeveloper(message, options, messages);
		return true;
	}
	if (message.role === "system") {
		const content = flattenText(message);
		if (content.length > 0) messages.push({
			role: "system",
			content,
			timestamp: 0
		});
		return true;
	}
	if (message.role === "assistant") {
		appendAssistant(message, messages, toolNames, onReplayDegrade);
		return true;
	}
	return false;
}
function textOnlyContext(options, onReplayDegrade) {
	assertSupportedHistory(options.messages);
	const split = splitSystemPrompt(options);
	const toolNames = /* @__PURE__ */ new Map();
	const messages = [];
	for (const message of split.messages) {
		if (contentHasImage(message.content)) throw new LlmError("pi-ai image conversion requires the durable attachment service", "UNSUPPORTED_CONTENT");
		if (appendSharedHistory(message, options, messages, toolNames, onReplayDegrade)) continue;
		if (message.role === "tool") {
			messages.push(toolResultOf(message, toolNames, flattenText(message)));
			continue;
		}
		messages.push({
			role: "user",
			content: flattenText(message),
			timestamp: 0
		});
	}
	return piContext(split.systemPrompt, options, messages);
}

function toPiContext(options, onReplayDegrade) { return textOnlyContext(options, onReplayDegrade); }

export { toPiReplayState, toPiAssistant, toPiContext };
