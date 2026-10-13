from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from json_repair import repair_json

ToolHandler = Callable[[dict[str, Any]], Any]
EventCallback = Callable[[dict[str, Any]], None]
RoundContextCallback = Callable[[int, int], str]
ReasoningEffort = str | Callable[[int], str | None] | None

POINT_MUTATION_TOOL_NAMES = {
    "create_knowledge_points",
    "append_knowledge_points",
    "add_point_aliases",
}


class AgentPause(Exception):
    """A tool-requested, recoverable pause that must escape the agent loop."""

    def __init__(self, reason: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.payload = payload or {}


@dataclass(slots=True)
class AgentRunResult:
    payload: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    duration_ms: int
    tool_trace: list[dict[str, Any]]


def run_tool_agent(
    *,
    client: Any,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict[str, Any]],
    handlers: dict[str, ToolHandler],
    final_tool_name: str,
    max_steps: int = 8,
    max_tokens: int = 20000,
    event_callback: EventCallback | None = None,
    tool_budget_steps: int | None = None,
    fallback_payload: dict[str, Any] | None = None,
    force_final_round: bool = False,
    error_recovery_round: bool = False,
    always_available_tool_names: set[str] | None = None,
    final_payload_validator: Callable[[dict[str, Any]], str | None] | None = None,
    resume_state: dict[str, Any] | None = None,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    round_plan: list[str] | None = None,
    round_context_callback: RoundContextCallback | None = None,
    visible_total_steps: int | None = None,
    final_tool_min_step: int = 1,
    provisional_final_step: int | None = None,
    expose_round_limits_to_model: bool = True,
    require_final_on_last_step: bool = True,
    model: str | None = None,
    reasoning_effort: ReasoningEffort = None,
) -> AgentRunResult:
    model = (model or getattr(client, "model", "")).strip()
    if not model:
        raise ValueError("Agent 调用必须显式提供当前配置的模型名称")
    resume_state = compact_agent_resume_state(resume_state)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    if resume_state:
        messages.extend(list(resume_state.get("messages_tail") or []))
    total_input = int((resume_state or {}).get("input_tokens", 0))
    total_output = int((resume_state or {}).get("output_tokens", 0))
    total_hit = int((resume_state or {}).get("cache_hit_tokens", 0))
    total_miss = int((resume_state or {}).get("cache_miss_tokens", 0))
    trace: list[dict[str, Any]] = list((resume_state or {}).get("tool_trace") or [])
    started = time.perf_counter()
    always_available_tool_names = always_available_tool_names or set()
    recovery_needed = bool((resume_state or {}).get("recovery_needed", False))
    recovery_used = bool((resume_state or {}).get("recovery_used", False))
    normal_total = max_steps + (1 if force_final_round else 0)
    hard_total = normal_total + (1 if force_final_round or error_recovery_round else 0)

    start_step = max(1, int((resume_state or {}).get("next_step", 1)))
    for step in range(start_step, hard_total + 1):
        if step > normal_total and not recovery_needed:
            break
        if step > normal_total:
            recovery_used = True
        # Keep the provider-visible tool schema byte-for-byte stable across all
        # rounds. Prompt caches are prefix based, and changing the tools array
        # invalidates the system/messages prefix even when the prose is unchanged.
        # Phase restrictions are enforced locally through allowed_tool_names.
        request_tools = tools
        allowed_tool_names = {
            tool.get("function", {}).get("name") for tool in tools
            if not (
                tool.get("function", {}).get("name") == final_tool_name
                and step < final_tool_min_step
            )
        }
        if tool_budget_steps is not None and step > tool_budget_steps:
            allowed = {final_tool_name, *always_available_tool_names}
            allowed_tool_names.intersection_update(allowed)
            if step == tool_budget_steps + 1:
                messages.append({
                    "role": "user",
                    "content": (
                        "外部研究阶段现在结束，不得再调用 RAG 或 Wiki 搜索。"
                        + (f"仍可批量使用必要的要点维护工具，并尽快调用 {final_tool_name}。"
                           if always_available_tool_names else f"请立即调用 {final_tool_name}。")
                    ),
                })
        if step > max_steps:
            allowed = {final_tool_name, *always_available_tool_names}
            allowed_tool_names.intersection_update(allowed)
        visible_total = hard_total if step > normal_total else (visible_total_steps or normal_total)
        phase = (round_plan[step - 1] if round_plan and step <= len(round_plan) else (
            "完整工作" if step <= max_steps - 3 else
            "收敛与批量补全" if step <= max_steps - 1 else
            "完成数据写入并准备提交" if step == max_steps else
            "错误恢复" if step > normal_total else "最终提交"
        ))
        if not expose_round_limits_to_model and not round_plan:
            phase = "依据实时 TODO 自主选择下一步"
        available_names = [
            tool.get("function", {}).get("name") for tool in tools
            if tool.get("function", {}).get("name") in allowed_tool_names
        ]
        request_tool_names = {
            tool.get("function", {}).get("name") for tool in request_tools
        }
        round_context = round_context_callback(step, visible_total) if round_context_callback else ""
        round_status = (
            f"轮次状态：第 {step}/{visible_total} 轮；阶段：{phase}；"
            f"本轮后剩余 {max(0, visible_total - step)} 轮。"
            if expose_round_limits_to_model else
            f"执行状态：继续完成当前目标；阶段建议：{phase}。"
        )
        messages.append({
            "role": "user",
            "content": (
                round_status
                +
                f"当前可用工具：{', '.join(str(name) for name in available_names)}。"
                "请在同一回复中并行发起互不依赖的工具调用，并让批量工具一次处理一组已经规划好的对象；"
                "严格遵守工具上限和本阶段建议，不要逐个查询、拖延、重复调查，也不要为了显得积极而一次倾倒全部对象。"
                + (f"本轮必须调用 {final_tool_name}。如确有遗漏，可先批量创建或追加，但同一回复中必须最后提交。"
                   if ((provisional_final_step is not None and step == provisional_final_step)
                       or (require_final_on_last_step and
                           (step > max_steps if force_final_round else step >= max_steps))) else "")
                + (f"\n\n{round_context}" if round_context else "")
            ),
        })
        try:
            current_reasoning_effort = (
                reasoning_effort(step) if callable(reasoning_effort) else reasoning_effort
            )
            if event_callback:
                event_callback({
                    "step": step, "max_steps": visible_total,
                    "event_type": "model_started", "status": "running",
                    "arguments": {},
                })
            response = client.complete_agent(
                model=model, messages=messages, tools=request_tools, max_tokens=max_tokens,
                reasoning_effort=current_reasoning_effort,
            )
        except ValueError as error:
            recoverable = "truncated" in str(error).casefold() or "截断" in str(error)
            if (force_final_round or error_recovery_round) and step >= max_steps and recoverable and not recovery_used:
                recovery_needed = True
                messages.append({
                    "role": "user",
                    "content": (
                        f"上一轮输出超过限制。额外错误恢复轮中只保留 {final_tool_name} "
                        "要求的必填字段，大幅压缩推理和文字，不得重述已写入的要点。"
                    ),
                })
                if event_callback:
                    event_callback({
                        "step": step, "max_steps": hard_total, "event_type": "agent_round_error",
                        "status": "rejected", "arguments": {"error": str(error)},
                        "result_preview": str(error),
                    })
                continue
            raise
        total_input += response.input_tokens
        total_output += response.output_tokens
        total_hit += response.cache_hit_tokens
        total_miss += response.cache_miss_tokens
        for call in response.tool_calls:
            repaired_name = normalize_tool_call_name(call.name, request_tool_names)
            if repaired_name == call.name:
                continue
            original_name = call.name
            call.name = repaired_name
            rewrite_assistant_tool_call(
                response.assistant_message, call.id, name=repaired_name,
            )
            trace.append({
                "step": step, "tool": repaired_name,
                "original_tool_name": original_name, "tool_name_repaired": True,
            })
            if event_callback:
                event_callback({
                    "step": step, "max_steps": visible_total,
                    "event_type": "tool_name_repaired", "tool_name": repaired_name,
                    "status": "completed",
                    "arguments": {"original_name": original_name, "repaired_name": repaired_name},
                    "result_preview": f"已将重复工具名 {original_name} 修复为 {repaired_name}。",
                })
        messages.append(response.assistant_message)
        if event_callback:
            event_callback({
                "step": step, "event_type": "model_completed", "status": "completed",
                "max_steps": visible_total,
                "tool_names": [call.name for call in response.tool_calls],
                "arguments": {
                    "finish_reason": response.finish_reason,
                    "content": response.content,
                    "content_chars": len(response.content),
                    "reasoning_content_chars": response.reasoning_content_chars,
                    "raw_tool_calls": [{
                        "id": call.id, "name": call.name, "arguments": call.arguments,
                        "arguments_chars": len(call.arguments),
                    } for call in response.tool_calls],
                },
                "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                "cache_hit_tokens": response.cache_hit_tokens, "cache_miss_tokens": response.cache_miss_tokens,
            })

        if not response.tool_calls:
            if response.content.strip():
                try:
                    payload = json.loads(strip_json_fence(response.content))
                    return finalize(payload, total_input, total_output, total_hit, total_miss, started, trace)
                except json.JSONDecodeError:
                    messages.append({
                        "role": "user",
                        "content": f"最终结果必须调用 {final_tool_name}，不要用普通文本回答。",
                    })
                    if (force_final_round or error_recovery_round) and step >= max_steps and not recovery_used:
                        recovery_needed = True
                    continue
            if (force_final_round or error_recovery_round) and step >= max_steps and not recovery_used:
                recovery_needed = True
                messages.append({
                    "role": "user",
                    "content": f"上一轮未返回工具调用。额外错误恢复轮必须调用 {final_tool_name}。",
                })
                continue
            raise ValueError("Agent returned neither tool calls nor final content")

        inactive_tool_seen = False
        # A provider may return parallel calls in any order. Resolve read-before-write
        # dependencies deterministically, then persist mutations before final submission.
        tool_priority = {
            "get_knowledge_points": 0,
            "get_volume_report": 0,
            "get_volume_summaries": 0,
            "get_content_summaries": 0,
            "search_knowledge_points": 0,
            "get_chapter_lists": 0,
            "get_chapter_report": 0,
            "get_chapter_summaries": 0,
            "get_wiki_entry": 0,
            "get_wiki_entries": 0,
            "search_original_text": 0,
            "search_wiki": 0,
            # A response may create templates and entries in parallel. Persist
            # templates first so template references validate deterministically.
            "upsert_wiki_templates": 1,
            "upsert_entry_blueprints": 2,
            "remove_entry_blueprints": 2,
            "create_knowledge_points": 1,
            "append_knowledge_points": 2,
            "add_point_aliases": 2,
            final_tool_name: 3,
        }
        ordered_calls = sorted(
            enumerate(response.tool_calls),
            key=lambda item: (tool_priority.get(item[1].name, 1), item[0]),
        )
        ordered_calls = [call for _, call in ordered_calls]
        round_had_error = False
        for call in ordered_calls:
            parse_error = ""
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as error:
                adjacent = decode_adjacent_argument_objects(call.arguments or "")
                repaired = (
                    merge_argument_objects(adjacent)
                    if len(adjacent) > 1
                    else repair_json(call.arguments or "{}", return_objects=True)
                )
                if isinstance(repaired, list) and repaired and all(isinstance(item, dict) for item in repaired):
                    repaired = merge_argument_objects(repaired)
                if isinstance(repaired, dict):
                    arguments = repaired
                    trace.append({"step": step, "tool": call.name, "arguments_repaired": True})
                    if event_callback:
                        event_callback({
                            "step": step, "max_steps": visible_total,
                            "event_type": "tool_arguments_repaired", "tool_name": call.name,
                            "status": "completed", "arguments": arguments,
                            "result_preview": f"本地修复 JSON：{error.msg} at position {error.pos}",
                            "result_size": len(call.arguments),
                        })
                else:
                    parse_error = f"{error.msg} at position {error.pos}"
                    arguments = {"__parse_error__": parse_error, "__raw_arguments__": call.arguments}
            if not parse_error:
                arguments, wrappers_changed = normalize_tool_arguments(arguments)
                if wrappers_changed:
                    trace.append({"step": step, "tool": call.name, "arguments_unwrapped": True})
                    if event_callback:
                        event_callback({
                            "step": step, "max_steps": visible_total,
                            "event_type": "tool_arguments_unwrapped", "tool_name": call.name,
                            "status": "completed", "arguments": arguments,
                            "result_preview": "已解开供应商返回的 $text JSON 包装。",
                            "result_size": len(json.dumps(arguments, ensure_ascii=False, default=str)),
                        })
            # Providers validate every historical assistant tool call again on the
            # following request. Persist only canonical JSON, otherwise a call that
            # was repaired and executed locally can still make the next round fail
            # before the model starts.
            history_arguments = (
                arguments if not parse_error else {"__parse_error__": parse_error}
            )
            rewrite_assistant_tool_call(
                response.assistant_message,
                call.id,
                name=call.name,
                arguments=json.dumps(history_arguments, ensure_ascii=False, default=str),
            )
            if parse_error and call.name != final_tool_name:
                result = {"accepted": False, "error": f"工具参数不是合法 JSON：{parse_error}"}
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
                round_had_error = True
                trace.append({
                    "step": step, "tool": call.name, "parse_error": parse_error,
                    "result_size": len(json.dumps(result, ensure_ascii=False)),
                })
                if event_callback:
                    event_callback({
                        "step": step, "event_type": "tool_result", "tool_name": call.name,
                        "max_steps": visible_total, "status": "rejected",
                        "arguments": {"parse_error": parse_error},
                        "result_preview": result["error"], "result_size": len(call.arguments or ""),
                    })
                continue
            if call.name == final_tool_name:
                if step < final_tool_min_step:
                    validation_error = (
                        f"{final_tool_name} 最早在第 {final_tool_min_step} 轮开放；"
                        "请先完成当前阶段的研究与草稿写入。"
                    )
                    messages.append({
                        "role": "tool", "tool_call_id": call.id,
                        "content": json.dumps({"accepted": False, "error": validation_error}, ensure_ascii=False),
                    })
                    round_had_error = True
                    if event_callback:
                        event_callback({
                            "step": step, "max_steps": visible_total, "event_type": "final_submission",
                            "tool_name": call.name, "status": "rejected", "arguments": arguments,
                            "result_preview": validation_error,
                        })
                    continue
                if parse_error:
                    messages.append({
                        "role": "tool", "tool_call_id": call.id,
                        "content": json.dumps({"accepted": False, "error": f"工具参数不是合法 JSON：{parse_error}"}, ensure_ascii=False),
                    })
                    round_had_error = True
                    continue
                validation_error = final_payload_validator(arguments) if final_payload_validator else None
                if validation_error:
                    messages.append({
                        "role": "tool", "tool_call_id": call.id,
                        "content": json.dumps({"accepted": False, "error": validation_error}, ensure_ascii=False),
                    })
                    round_had_error = True
                    if event_callback:
                        event_callback({
                            "step": step, "max_steps": visible_total, "event_type": "final_submission",
                            "tool_name": call.name, "status": "rejected", "arguments": arguments,
                            "result_preview": validation_error,
                        })
                    continue
                if provisional_final_step is not None and step == provisional_final_step:
                    provisional_result = {
                        "accepted": True,
                        "provisional": True,
                        "message": (
                            "预提交已记录，但本次运行不会在此结束。下一轮必须读取最新软审计，"
                            "进行自我审查、清理残余内容、修正页面与链接后再次最终提交。"
                        ),
                    }
                    trace.append({
                        "step": step, "tool": call.name, "provisional_final": True,
                        "payload_keys": list(arguments)[:30],
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": call.id,
                        "content": json.dumps(provisional_result, ensure_ascii=False),
                    })
                    if event_callback:
                        event_callback({
                            "step": step, "event_type": "final_submission", "tool_name": call.name,
                            "max_steps": visible_total, "status": "completed", "arguments": arguments,
                            "result_preview": provisional_result["message"],
                            "result_size": len(json.dumps(arguments, ensure_ascii=False)),
                        })
                    continue
                trace.append({
                    "step": step, "tool": call.name, "final": True,
                    "forced": step > max_steps, "payload_keys": list(arguments)[:30],
                })
                if event_callback:
                    event_callback({
                        "step": step, "event_type": "final_submission", "tool_name": call.name,
                        "max_steps": visible_total,
                        "status": "accepted", "arguments": arguments,
                        "result_size": len(json.dumps(arguments, ensure_ascii=False)),
                    })
                return finalize(arguments, total_input, total_output, total_hit, total_miss, started, trace)
            handler = handlers.get(call.name) if call.name in allowed_tool_names else None
            if call.name not in allowed_tool_names:
                inactive_tool_seen = True
                result: Any = {
                    "error": f"Tool {call.name} is no longer available; call {final_tool_name} now."
                }
            elif handler is None:
                result = {"error": f"Unknown tool: {call.name}"}
            else:
                try:
                    result = handler(arguments)
                except AgentPause:
                    raise
                except Exception as error:
                    result = {"error": f"{type(error).__name__}: {str(error)[:300]}"}
            round_had_error = round_had_error or result_has_error(result)
            trace.append({
                "step": step,
                "tool": call.name,
                "arguments": arguments,
                "result_size": len(json.dumps(result, ensure_ascii=False, default=str)),
            })
            if event_callback:
                result_stats = summarize_result_stats(result, call.name)
                event_callback({
                    "step": step, "event_type": "tool_result", "tool_name": call.name,
                    "max_steps": visible_total,
                    "status": "completed", "arguments": arguments,
                    "result_preview": compact_preview(result),
                    "result_size": len(json.dumps(result, ensure_ascii=False, default=str)),
                    "result_stats": result_stats,
                })
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(
                    compact_point_mutation_result(result)
                    if call.name in POINT_MUTATION_TOOL_NAMES else result,
                    ensure_ascii=False,
                    default=str,
                ),
            })
        if inactive_tool_seen:
            messages.append({
                "role": "user",
                "content": f"不要继续研究。现在只调用 {final_tool_name} 提交已有结论。",
            })
        if checkpoint_callback:
            checkpoint_callback({
                "next_step": step + 1,
                "messages_tail": messages[2:],
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cache_hit_tokens": total_hit,
                "cache_miss_tokens": total_miss,
                "tool_trace": trace,
                "recovery_needed": recovery_needed or step >= normal_total,
                "recovery_used": recovery_used,
            })
        if step >= normal_total and round_had_error and not recovery_used:
            recovery_needed = True
        elif step >= normal_total and not recovery_used:
            recovery_needed = True

    if fallback_payload is not None:
        trace.append({"step": max_steps, "tool": final_tool_name, "final": True, "fallback": True})
        if event_callback:
            event_callback({
                "step": max_steps, "event_type": "agent_fallback", "status": "completed",
                "max_steps": normal_total,
                "arguments": fallback_payload,
                "result_preview": f"Agent 未主动提交 {final_tool_name}；使用安全降级结果继续。",
            })
        return finalize(
            fallback_payload, total_input, total_output, total_hit, total_miss, started, trace
        )
    raise ValueError(f"Agent did not successfully call {final_tool_name} within the allowed rounds")


def finalize(
    payload: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    started: float,
    trace: list[dict[str, Any]],
) -> AgentRunResult:
    return AgentRunResult(
        payload=payload,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        duration_ms=round((time.perf_counter() - started) * 1000),
        tool_trace=trace,
    )


def inline_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve model-local definitions before embedding schemas in tools.

    Pydantic's #/$defs pointers belong to the model schema, not the containing
    function parameters. Inline them while that local root is still available.
    """
    def visit(value: Any, root: dict[str, Any], resolving: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [visit(item, root, resolving) for item in value]
        if not isinstance(value, dict):
            return value
        if "$defs" in value:
            root = value
        reference = value.get("$ref")
        if reference:
            if not reference.startswith("#/") or reference in resolving:
                raise ValueError(f"Unsupported tool schema reference: {reference}")
            target: Any = root
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            resolved = visit(target, root, (*resolving, reference))
            siblings = {key: item for key, item in value.items() if key != "$ref"}
            return {**resolved, **visit(siblings, root, resolving)}
        return {key: visit(item, root, resolving) for key, item in value.items() if key != "$defs"}

    return visit(schema, schema)


def function_tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": inline_tool_schema(properties),
                "required": required or [],
                "additionalProperties": True,
            },
        },
    }


def strip_json_fence(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
    return cleaned


def compact_agent_resume_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact durable Agent history without dropping research evidence.

    Read tools intentionally retain their complete results. Mutation tools only
    need to confirm what was accepted: replaying every point's complete history
    duplicates data the model has just submitted and can add hundreds of
    thousands of characters to the next request.
    """
    if not state:
        return state
    compacted = dict(state)
    compacted["messages_tail"] = compact_agent_message_history(
        list(state.get("messages_tail") or [])
    )
    return compacted


def compact_agent_message_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tool_names: dict[str, str] = {}
    compacted: list[dict[str, Any]] = []
    for original in messages:
        message = dict(original)
        if message.get("role") == "assistant":
            normalized_calls = []
            for original_call in message.get("tool_calls") or []:
                call = dict(original_call) if isinstance(original_call, dict) else original_call
                if not isinstance(call, dict):
                    normalized_calls.append(call)
                    continue
                function = dict(call.get("function") or {})
                call_id = str(call.get("id") or "")
                if call_id and isinstance(function, dict):
                    function["name"] = normalize_tool_call_name(str(function.get("name") or ""))
                    function["arguments"] = canonical_tool_arguments(function.get("arguments"))
                    call["function"] = function
                    tool_names[call_id] = function["name"]
                normalized_calls.append(call)
            message["tool_calls"] = normalized_calls
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if tool_names.get(call_id) in POINT_MUTATION_TOOL_NAMES:
                try:
                    payload = json.loads(str(message.get("content") or "{}"))
                except (TypeError, ValueError):
                    payload = None
                if payload is not None:
                    message["content"] = json.dumps(
                        compact_point_mutation_result(payload), ensure_ascii=False, default=str
                    )
        compacted.append(message)
    return compacted


def canonical_tool_arguments(value: Any) -> str:
    """Return a provider-safe JSON object string for durable tool-call history."""
    raw = value if isinstance(value, str) else json.dumps(value or {}, ensure_ascii=False, default=str)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        parsed = repair_json(raw or "{}", return_objects=True)
    if not isinstance(parsed, dict):
        parsed = {}
    parsed, _ = normalize_tool_arguments(parsed)
    return json.dumps(parsed, ensure_ascii=False, default=str)


def normalize_tool_call_name(name: str, active_names: set[str] | None = None) -> str:
    """Repair a provider occasionally returning ``tool_nametool_name``."""
    candidates = active_names or set()
    for candidate in candidates:
        if candidate and name == candidate + candidate:
            return candidate
    midpoint = len(name) // 2
    if len(name) % 2 == 0 and name[:midpoint] == name[midpoint:]:
        return name[:midpoint]
    return name


def rewrite_assistant_tool_call(
    message: dict[str, Any],
    call_id: str,
    *,
    name: str | None = None,
    arguments: str | None = None,
) -> None:
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict) or str(call.get("id") or "") != call_id:
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        if name is not None:
            function["name"] = name
        if arguments is not None:
            function["arguments"] = arguments
        return


def compact_point_mutation_result(value: Any) -> Any:
    """Return the minimum lossless acknowledgement for point mutations."""
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        return value
    results: list[Any] = []
    for original in value["results"]:
        if not isinstance(original, dict):
            results.append(original)
            continue
        item = {
            key: original[key]
            for key in (
                "accepted", "error", "point_id", "replaced_current_volume_update"
            )
            if key in original
        }
        for source_key in ("point", "existing"):
            source = original.get(source_key)
            if not isinstance(source, dict):
                continue
            item[source_key] = {
                key: source[key]
                for key in ("id", "name", "canonical_name", "type", "point_type", "aliases")
                if key in source
            }
        results.append(item)
    return {**{key: item for key, item in value.items() if key != "results"}, "results": results}


def normalize_tool_arguments(value: Any) -> tuple[Any, bool]:
    """Unwrap MiniMax's ``{"$text": "{...}"}`` representation.

    MiniMax emits this form when an array's item schema is underspecified. We
    still send precise schemas, but accepting the wrapper keeps interrupted
    runs recoverable and prevents one malformed nested item from discarding a
    whole volume.
    """
    changed = False
    if isinstance(value, list):
        result = []
        for item in value:
            normalized, item_changed = normalize_tool_arguments(item)
            result.append(normalized); changed = changed or item_changed
        return result, changed
    if not isinstance(value, dict):
        return value, False
    if set(value) == {"$text"} and isinstance(value["$text"], str):
        raw = value["$text"].strip()
        if raw.startswith(("{", "[")):
            parsed = repair_json(raw, return_objects=True)
            if isinstance(parsed, (dict, list)):
                normalized, _ = normalize_tool_arguments(parsed)
                return normalized, True
        return raw, True
    result = {}
    for key, item in value.items():
        normalized, item_changed = normalize_tool_arguments(item)
        result[key] = normalized; changed = changed or item_changed
    return result, changed


def merge_argument_objects(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Recover adjacent JSON objects emitted for one tool call.

    Providers occasionally concatenate two valid argument objects. Nested
    dictionaries are merged, arrays are appended, and later scalar values
    replace earlier ones. This preserves independent batched writes without
    guessing which complete object should be discarded.
    """
    merged: dict[str, Any] = {}
    for item in items:
        for key, value in item.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged[key] = merge_argument_objects([current, value])
            elif isinstance(current, list) and isinstance(value, list):
                merged[key] = [*current, *value]
            else:
                merged[key] = value
    return merged


def decode_adjacent_argument_objects(raw: str) -> list[dict[str, Any]]:
    """Decode multiple valid top-level JSON objects concatenated in one string."""
    decoder = json.JSONDecoder()
    index = 0
    objects: list[dict[str, Any]] = []
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        try:
            value, index = decoder.raw_decode(raw, index)
        except json.JSONDecodeError:
            return []
        if not isinstance(value, dict):
            return []
        objects.append(value)
    return objects


def compact_preview(value: Any, limit: int = 1200) -> str:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    return raw if len(raw) <= limit else raw[:limit] + "…"


def summarize_result_stats(value: Any, tool_name: str = "") -> dict[str, Any]:
    """Keep compact, structured result counts even when the JSON preview is truncated."""
    if not isinstance(value, dict):
        return {}
    if tool_name in {"search_original_text", "search_wiki"}:
        if isinstance(value.get("queries"), list):
            queries = [item for item in value["queries"] if isinstance(item, dict)]
            return {
                "queries": len(queries),
                "returned": sum(len(item.get("matches") or item.get("results") or []) for item in queries),
            }
        matches = value.get("matches")
        if not isinstance(matches, list):
            matches = value.get("results")
        return {"returned": len(matches) if isinstance(matches, list) else 0}
    if tool_name == "get_content_summaries" and isinstance(value.get("results"), list):
        return {"returned": int(value.get("returned") or 0), "missing": int(value.get("errors") or 0)}
    if tool_name == "get_chapter_lists" and isinstance(value.get("volumes"), list):
        return {
            "returned": int(value.get("returned") or 0), "missing": int(value.get("errors") or 0),
            "chapters": sum(len(item.get("chapters") or []) for item in value["volumes"] if isinstance(item, dict)),
        }
    if tool_name == "search_knowledge_points" and isinstance(value.get("queries"), list):
        queries = [item for item in value["queries"] if isinstance(item, dict)]
        return {
            "queries": len(queries),
            "returned": sum(len(item.get("points") or []) for item in queries),
            "missing": sum(bool(item.get("error")) or not item.get("points") for item in queries),
        }
    if tool_name == "get_volume_summaries" and isinstance(value.get("summaries"), list):
        return {"returned": len(value["summaries"]), "missing": len(value.get("missing") or [])}
    if tool_name == "get_chapter_summaries" and isinstance(value.get("chapters"), list):
        return {"returned": len(value["chapters"]), "missing": len(value.get("missing") or [])}
    if tool_name == "search_chapter_titles" and isinstance(value.get("results"), list):
        return {"returned": len(value["results"])}
    if tool_name == "get_full_chapter":
        return {
            "returned": 1 if value.get("content") else 0,
            "used": int(value.get("used") or 0),
            "characters_used": int(value.get("characters_used") or 0),
            "call_threshold": int(value.get("call_threshold") or 3),
            "character_threshold": int(value.get("character_threshold") or 30_000),
            "disabled": bool(value.get("disabled") or value.get("disabled_after_this_call")),
        }
    if tool_name in {"upsert_entry_blueprints", "upsert_special_blueprints", "upsert_category_pages", "upsert_wiki_templates"}:
        if value.get("error"):
            return {
                "error": str(value["error"]),
                "submitted_count": int(value.get("submitted_count") or 0),
                "limit": int(value.get("limit") or 0),
            }
        results = [item for item in value.get("results") or [] if isinstance(item, dict)]
        accepted = [item for item in results if item.get("accepted")]
        return {
            "total": len(results), "accepted": len(accepted),
            "rejected": len(results) - len(accepted),
            "replaced": sum(bool(item.get("replaced")) for item in accepted),
            "names": [str(item.get("slug") or item.get("id") or "") for item in accepted[:8]],
        }
    if tool_name == "get_wiki_entries" and isinstance(value.get("entries"), list):
        return {
            "returned": len(value["entries"]),
            "planned": len(value.get("planned_uncompiled") or []),
            "missing": len(value.get("missing") or value.get("missing_or_uncompiled") or []),
            "names": [str(item.get("title") or item.get("slug") or "")
                      for item in value["entries"][:8] if isinstance(item, dict)],
        }
    if isinstance(value.get("points"), list):
        points = [item for item in value["points"] if isinstance(item, dict)]
        return {
            "returned": len(points),
            "missing": len(value.get("missing_ids") or []),
            "names": [str(item.get("name") or item.get("canonical_name") or "") for item in points[:8]],
        }
    if isinstance(value.get("results"), list):
        results = [item for item in value["results"] if isinstance(item, dict)]
        accepted = [item for item in results if item.get("accepted")]
        return {
            "total": len(results),
            "accepted": len(accepted),
            "rejected": len(results) - len(accepted),
            "replaced": sum(bool(item.get("replaced_current_volume_update")) for item in accepted),
            "names": [
                str((item.get("point") or {}).get("name") or (item.get("point") or {}).get("canonical_name") or "")
                for item in accepted[:8]
            ],
        }
    return {}


def result_has_error(value: Any) -> bool:
    if isinstance(value, list):
        return any(result_has_error(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("error") or value.get("accepted") is False:
        return True
    return any(result_has_error(item) for item in value.values())
