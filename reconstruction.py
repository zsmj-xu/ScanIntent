"""Deterministic conversation reconstruction from repeated model-call snapshots."""

import hashlib
import json
import re
from difflib import SequenceMatcher

from clickhouse import json_text


CONFIRMATION_TOOLS = {"askuserquestion", "requestuserinput", "exitplanmode", "askuser"}
SUMMARY_PREFIXES = ("this session is being continued from a previous conversation",
                    "this conversation was compacted", "<context_summary>",
                    "<conversation_summary>",
                    "another language model started working on this problem and produced a summary",
                    "another language model started to solve this problem and produced a summary")
DIALOGUE_KINDS = {"text", "confirmation_request", "confirmation_answer", "attachment"}
INTERNAL_KINDS = {"context", "context_summary", "compaction_request", "call_error"}


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def text_content(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(b.get("text", b.get("content", "")))
                         if isinstance(b, dict) else text_content(b) for b in value)
    return json_text(value)


def tool_result_text(value):
    if not isinstance(value, list):
        return text_content(value)
    parts = []
    for block in value:
        if isinstance(block, dict) and block.get("type") in ("image", "image_url", "input_image"):
            parts.append("[工具返回图片]")
        else:
            parts.append(text_content(block))
    return "\n".join(part for part in parts if part)


def auxiliary(inp):
    messages = inp.get("messages", []) if isinstance(inp, dict) else []
    text = "\n".join(text_content(m.get("content")) for m in messages if isinstance(m, dict))
    last = text_content(messages[-1].get("content")) if messages else ""
    policy_tail = text.rsplit("</transcript>", 1)[-1].strip() if "<transcript>" in text and "</transcript>" in text else ""
    policy_call = policy_tail.startswith(("Err on the side of blocking. Stage 1", "Review the classification process and follow it carefully"))
    return (policy_call or ("<transcript>" in text and "Your ENTIRE response MUST begin with <block>" in text)
            or (text.lstrip().startswith("<session>") and "Write the title in" in text)
            or last.lstrip().startswith("[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]")
            or last.startswith("The user stepped away and is coming back. Recap in under 40 words,"))


def user_texts(inp):
    messages = inp.get("messages", []) if isinstance(inp, dict) else []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else [dict(text=content)]
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                yield block["text"].strip()


def title_candidate(inp, output):
    """Require a title-only schema and result, not merely the word 'title'."""
    if not isinstance(inp, dict) or inp.get("tools"):
        return False
    result = decode(output.get("content")) if isinstance(output, dict) else decode(output)
    if isinstance(result, str):
        fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", result, re.S)
        if fenced:
            result = decode(fenced.group(1))
    if not isinstance(result, dict) or set(result) != {"title"} or not isinstance(result["title"], str):
        return False
    for text in user_texts(inp):
        schema = decode(text)
        if (isinstance(schema, dict) and schema.get("type") == "object"
                and set(schema.get("properties", {})) == {"title"}
                and schema.get("required") == ["title"]):
            return True
    return False


def compaction_span(text):
    for match in re.finditer(r"(?:^|\n)(CRITICAL: Respond with TEXT ONLY\.|Your task is to create a detailed summary of the conversation so far)", text):
        start = match.start(1)
        suffix = text[start:]
        summary_task = "Your task is to create a detailed summary of the conversation so far" in suffix
        protocol = (("<analysis>" in suffix and "<summary>" in suffix)
                    or ("wrap your analysis in <analysis> tags" in suffix and "Your summary should include" in suffix))
        if summary_task and protocol:
            return start, len(text)
    return None


def compaction_request(text):
    return compaction_span(text) is not None


def tool_calls(value):
    messages = value.get("messages", [value]) if isinstance(value, dict) else value if isinstance(value, list) else []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            yield call.get("id", ""), function.get("name", "").rsplit(".", 1)[-1], decode(function.get("arguments"))
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block.get("id", ""), block.get("name", "").rsplit(".", 1)[-1], block.get("input")


def classify_branches(prepared, call_kinds):
    """Only explicit Agent/Task prompts or WebSearch queries establish a branch."""
    prompts, owners, summaries, branches, tools = {}, {}, {}, {}, {}
    main_root = None
    for index, (_, inp, out) in enumerate(prepared):
        if call_kinds.get(index) in ("title", "auxiliary"):
            continue
        texts = list(user_texts(inp))
        if main_root is None and texts:
            main_root = texts[0]
        matches = {prompts[text] for text in texts if text in prompts and text != main_root}
        branch = next(iter(matches)) if len(matches) == 1 else None
        if branch is None and not matches:
            anchors = {owners[call_id] for call_id, _, _ in tool_calls(inp) if call_id in owners}
            if len(anchors) == 1 and main_root not in texts:
                branch = next(iter(anchors))
            if branch is None and main_root not in texts:
                for summary, owner in summaries.items():
                    if summary and any(summary in text for text in texts):
                        branch = owner
                        break
        if branch:
            branches[index] = branch
        for call_id, name, args in list(tool_calls(inp)) + list(tool_calls(out)):
            if branch and call_id:
                owners[call_id] = branch
            if name in ("Agent", "Task", "WebSearch") and isinstance(args, dict) and call_id:
                prompt = args.get("prompt") if name != "WebSearch" else args.get("query")
                if isinstance(prompt, str) and prompt.strip():
                    key = prompt.strip() if name != "WebSearch" else "Perform a web search for the query: " + prompt.strip()
                    prompts.setdefault(key, call_id)
                    tools[call_id] = name
        if branch and call_kinds.get(index) == "compaction":
            content = text_content(out.get("content")) if isinstance(out, dict) else text_content(out)
            match = re.search(r"<summary>(.*?)</summary>", content, re.S)
            if match:
                summaries[match.group(1).strip()[:200]] = branch
    return branches, tools


def classify_calls(prepared):
    """Use the following main-history snapshot to corroborate auxiliary calls."""
    candidates = {i for i, (_, inp, out) in enumerate(prepared) if title_candidate(inp, out)}
    prompt_positions = {}
    for i, (_, inp, _) in enumerate(prepared):
        if i not in candidates and isinstance(inp, dict) and (inp.get("tools") or len(inp.get("messages", [])) > 1):
            for text in user_texts(inp):
                prompt_positions[text] = i
    kinds = {}
    for i, (_, inp, out) in enumerate(prepared):
        if auxiliary(inp):
            kinds[i] = "auxiliary"
        elif i in candidates and any(prompt_positions.get(re.sub(r"^<session>\s*|\s*</session>$", "", text).strip(), -1) > i for text in user_texts(inp)):
            kinds[i] = "title"
        messages = inp.get("messages", []) if isinstance(inp, dict) else []
        users = [message for message in messages if isinstance(message, dict) and message.get("role") == "user"]
        if not users:
            continue
        request = next((text for text in user_texts({"messages": [users[-1]]})
                        if compaction_request(text)), "")
        # The full runtime template also identifies empty/failed compaction retries.
        if request and kinds.get(i) not in ("title", "auxiliary"):
            kinds[i] = "compaction"
    return kinds


def call_error(observation, output):
    status = observation.get("status_message") or text_content(output.get("content") if isinstance(output, dict) else output)
    match = re.match(r"([A-Za-z_][A-Za-z0-9_.]{0,79}(?:Error|Exception|Timeout))\b", status or "")
    category = match.group(1) if match else "ERROR"
    return dict(message_id=hashlib.sha256((observation["observation_id"] + ":call_error").encode()).hexdigest()[:24],
                role="system", kind="call_error", content="模型调用失败：" + category,
                observation_id=observation["observation_id"],
                field="status_message" if observation.get("status_message") else "output",
                path="" if observation.get("status_message") or not isinstance(output, dict) else "/content",
                observed_ms=int(observation.get("start_ms", 0)), classification_reason="source_error_level")


def dialogue_messages(events):
    """Group visible blocks of one original message without merging separate turns."""
    groups = []
    last_key = None
    for event in events:
        if event["kind"] not in DIALOGUE_KINDS:
            continue
        match = re.match(r"^(/messages/\d+|/\d+)(?:/|$)", event["path"])
        message_path = match.group(1) if match else ""
        key = (event["observation_id"], event["field"], message_path, event["role"])
        if not groups or key != last_key:
            groups.append(dict(message_id=event["message_id"], role=event["role"], kind=event["kind"],
                               observation_id=event["observation_id"], content="", parts=[]))
        group = groups[-1]
        group["parts"].append(dict(event))
        group["content"] = "\n\n".join(part["content"] for part in group["parts"])
        if event["kind"] in ("confirmation_request", "confirmation_answer"):
            group["kind"] = event["kind"]
        last_key = key
    return groups


def input_signature(inp):
    events = normalize(inp, {"observation_id": "signature"}, "input")
    return hashlib.sha256(json_text([fingerprint(event) for event in events]).encode()).hexdigest()


def classify_client_history(snapshot, inp, literal_replies):
    """Retain known Claude client control records without calling them dialogue."""
    messages = inp.get("messages", []) if isinstance(inp, dict) else []
    for event in snapshot:
        if event["kind"] != "text" or event["content"].strip() not in (
                "No response requested.", "Continue from where you left off.", "Tool loaded."):
            continue
        position = re.match(r"^/messages/(\d+)/", event["path"])
        index = int(position.group(1)) if position else None
        if event["content"].strip() == "Tool loaded.":
            if index is not None and index > 0 and any(name == "ToolSearch" for _, name, _ in tool_calls(messages[index - 1])):
                event.update(kind="context", classification_reason="client_tool_loaded")
            continue
        recorded_reply = bool(literal_replies) and index is not None and isinstance(messages, list) and input_signature({"messages": messages[:index]}) in literal_replies
        if (event["role"] == "assistant" and event["kind"] == "text"
                and event["content"].strip() == "No response requested."
                and not recorded_reply):
            event.update(kind="context", classification_reason="client_no_response_placeholder")
        if event["role"] != "user" or event["kind"] != "text" or event["content"].strip() != "Continue from where you left off.":
            continue
        if index is None or not isinstance(messages, list):
            continue
        if index + 1 >= len(messages):
            continue
        following = messages[index + 1]
        texts = list(user_texts({"messages": [messages[index]]}))
        has_body = any(text and text != "Continue from where you left off." and
                       not text.startswith(("<system-reminder>", "<local-command-caveat>")) for text in texts)
        if (has_body and following.get("role") == "assistant"
                and text_content(following.get("content")).strip() == "No response requested."
                and input_signature({"messages": messages[:index + 1]}) not in literal_replies):
            event.update(kind="context", classification_reason="client_resume_control")




def normalize(value, observation, field):
    if value is None:
        return []
    if isinstance(value, dict) and "messages" in value:
        messages = value["messages"]
        prefix = "/messages/"
    elif isinstance(value, list):
        messages = value
        prefix = "/"
    elif isinstance(value, dict) and ("content" in value or "tool_calls" in value):
        messages = [value]
        prefix = None
    else:
        messages = [{"role": "assistant" if field == "output" else "system",
                     "content": value if isinstance(value, str) else json_text(value)}]
        prefix = None
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    events = []
    tool_names = {}
    image_tool_result = False

    def emit(role, kind, content, path, **extra):
        if content == "" and kind == "text":
            return
        identity = observation["observation_id"] + ":" + field + ":" + path + ":" + str(len(events))
        events.append(dict(message_id=hashlib.sha256(identity.encode()).hexdigest()[:24],
                           role=role, kind=kind, content=content,
                           observation_id=observation["observation_id"], field=field, path=path,
                           observed_ms=int(observation.get("start_ms", 0)), **extra))

    def emit_text(role, text, path):
        if not isinstance(text, str):
            raise ValueError("text content must be a string")
        # Preserve injected context separately rather than showing it as human speech.
        parts = re.split(r"(<system-reminder>.*?</system-reminder>|"
                         r"<environment_context>.*?</environment_context>|"
                         r"<task-notification>.*?</task-notification>|"
                         r"<bash-input>.*?</bash-input>|<bash-stdout>.*?</bash-stdout>|<bash-stderr>.*?</bash-stderr>|"
                         r"<hetao-cloudcli-user-resources>.*?</hetao-cloudcli-user-resources>)", text, flags=re.S)
        offset = 0
        for index, part in enumerate(parts):
            span = [offset, offset + len(part)]
            offset += len(part)
            if not part.strip():
                continue
            if field == "input" and role == "user" and part.strip() in (
                    "[Request interrupted by user]", "[Request interrupted by user for tool use]"):
                emit(role, "context", part, path, segment=index, source_span=span,
                     classification_reason="client_interruption_control")
                continue
            interrupt = re.match(r"(?:<system-reminder>\s*)?The user sent a new message while you were working:\s*\n", part)
            if role in ("system", "developer") or part.startswith("<system-reminder>"):
                markers = ("\n\nIMPORTANT: After completing your current task, you MUST address the user's message above.",
                           "\n\nThis is how Claude Code surfaces messages the user sends mid-turn")
                boundaries = [part.find(marker, interrupt.end()) for marker in markers] if interrupt else []
                boundaries = [position for position in boundaries if position >= 0]
                if boundaries:
                    end = min(boundaries)
                    emit(role, "context", part[:interrupt.end()], path, segment=index, source_span=[span[0], span[0] + interrupt.end()])
                    emit("user", "text", part[interrupt.end():end], path, segment=index,
                         source_span=[span[0] + interrupt.end(), span[0] + end], classification_reason="client_mid_turn_user_message")
                    emit(role, "context", part[end:], path, segment=index, source_span=[span[0] + end, span[1]])
                    continue
            image_note = re.match(r"^\[Image: original \d+x\d+, displayed at \d+x\d+\.[^\]]*\]", part)
            if role == "user" and image_tool_result and image_note:
                boundary = span[0] + image_note.end()
                emit(role, "context", image_note.group(0), path, segment=index,
                     source_span=[span[0], boundary], classification_reason="image_tool_description")
                remainder = part[image_note.end():]
                if remainder.strip():
                    emit(role, "text", remainder, path, segment=index, source_span=[boundary, span[1]])
                continue
            if role == "user" and part.startswith("<hetao-cloudcli-user-resources>"):
                manifest = decode(part.split(">", 1)[1].rsplit("</", 1)[0].strip())
                files = manifest.get("resources") if isinstance(manifest, dict) else None
                if isinstance(files, list) and files and all(isinstance(f, dict) and isinstance(f.get("fileName"), str) for f in files):
                    for file_index, file in enumerate(files):
                        # Upload URLs and pod paths are runtime metadata, not dialogue.
                        emit(role, "attachment", file["fileName"], path, segment=index, source_span=span,
                             manifest_file_index=file_index, content_type=file.get("contentType", ""),
                             classification_reason="user_upload_manifest")
                    continue
            kind = "text"
            if part.startswith(("<system-reminder>", "<environment_context>", "<task-notification>",
                                "<bash-input>", "<bash-stdout>", "<bash-stderr>")) or part.lstrip().startswith(
                    ("<command-name>", "<local-command-stdout>", "<local-command-stderr>",
                     "<local-command-caveat>", "Base directory for this skill:")):
                kind = "context"
            elif part.startswith("# AGENTS.md instructions for ") and "<INSTRUCTIONS>" in part and "</INSTRUCTIONS>" in part:
                kind = "context"
            elif part.lstrip().lower().startswith(SUMMARY_PREFIXES):
                kind = "context_summary"
            elif role in ("system", "developer"):
                kind = "context"
            emit(role, kind, part, path, segment=index, source_span=span)

    def call(name, arguments, tool_id, path, role="assistant"):
        if not isinstance(arguments, (dict, list)):
            arguments = decode(arguments)
        short_name = name.rsplit(".", 1)[-1]
        if tool_id:
            tool_names[tool_id] = short_name.lower()
        kind = "confirmation_request" if re.sub(r"[^a-z]", "", short_name.lower()) in CONFIRMATION_TOOLS else "tool_call"
        emit(role, kind, json_text(arguments), path, tool_name=name, tool_call_id=tool_id)

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError("message must be an object")
        role = message.get("role", "assistant" if field == "output" else "user")
        path = prefix + str(index) if prefix is not None else ""
        content = message.get("content")
        image_tool_result = False
        if role in ("tool", "function"):
            emit("tool", "tool_result", tool_result_text(content), path + "/content",
                 tool_call_id=message.get("tool_call_id", message.get("name", "")))
        elif isinstance(content, list):
            for bi, block in enumerate(content):
                bp = path + "/content/" + str(bi)
                if not isinstance(block, dict):
                    emit_text(role, str(block), bp)
                    continue
                kind = block.get("type", "text")
                if kind in ("text", "input_text", "output_text"):
                    emit_text(role, block.get("text", ""), bp + "/text")
                elif kind == "tool_use":
                    call(block.get("name", ""), block.get("input", {}), block.get("id", ""), bp, role)
                elif kind == "tool_result":
                    emit("tool", "tool_result", tool_result_text(block.get("content")), bp,
                         tool_call_id=block.get("tool_use_id", ""), is_error=bool(block.get("is_error", False)))
                    image_tool_result = (not block.get("is_error") and tool_names.get(block.get("tool_use_id"))
                                         in ("read", "view_image", "read_image"))
                elif kind in ("compaction", "summary"):
                    emit("system", "context_summary", text_content(block.get("summary", block.get("content", block.get("text", "")))), bp)
                elif kind not in ("thinking", "redacted_thinking"):
                    emit(role, "attachment", json_text(block), bp)
        elif content is not None:
            emit_text(role, content if isinstance(content, str) else json_text(content), path + "/content")
        for ti, tool in enumerate(message.get("tool_calls") or []):
            function = tool.get("function") or {}
            call(function.get("name", ""), function.get("arguments", {}), tool.get("id", ""),
                 path + "/tool_calls/" + str(ti))
    return events


def fingerprint(event):
    if event["kind"] in ("tool_call", "confirmation_request") and event.get("tool_call_id"):
        # Providers may rewrite arguments in the next input; the call ID is stable.
        return json_text({k: event.get(k) for k in ("role", "kind", "tool_name", "tool_call_id")})
    values = {k: event.get(k) for k in ("role", "kind", "content", "tool_name", "tool_call_id")}
    if isinstance(values["content"], str):
        values["content"] = values["content"].strip()
    return json_text(values)


def merge_snapshot(history, snapshot, warnings):
    if not snapshot:
        return history
    if not history:
        return snapshot
    matcher = SequenceMatcher(a=[fingerprint(m) for m in history],
                              b=[fingerprint(m) for m in snapshot], autojunk=False)
    # Only user/assistant text or tool identities can establish continuity.
    meaningful = any(history[a + offset]["kind"] not in INTERNAL_KINDS
                     for a, b, size in matcher.get_matching_blocks() for offset in range(size))
    if not meaningful:
        if any(m["kind"] == "context_summary" for m in snapshot):
            warnings.add("compacted_context")
        else:
            warnings.add("unlinked_snapshot")
        return history + snapshot
    merged = []
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op in ("equal", "delete", "replace"):
            merged.extend(history[a1:a2])
        if op in ("insert", "replace"):
            merged.extend(snapshot[b1:b2])
        if op == "replace" and any(m["kind"] not in INTERNAL_KINDS for m in history[a1:a2]):
            warnings.add("sequence_divergence")
    return merged


def reconstruct(project, trace, observations):
    history, warnings, aliases, ids = [], set(), set(), []
    first_ms, last_ms, event_ms = None, 0, 0
    prepared = [(o, decode(o.get("input")), decode(o.get("output"))) for o in observations]
    call_kinds = classify_calls(prepared)
    branch_kinds, branch_tools = classify_branches(prepared, call_kinds)
    histories = {None: []}
    literal_replies = set()
    # Streaming source already orders records. Reject an accidentally mixed trace.
    for index, (observation, inp, output) in enumerate(prepared):
        if observation.get("trace_id", trace) != trace:
            raise ValueError("Mixed trace identities")
        oid = observation["observation_id"]
        ids.append(oid)
        start = int(observation.get("start_ms", 0))
        first_ms = start if first_ms is None else min(first_ms, start)
        last_ms = max(last_ms, start)
        event_ms = max(event_ms, int(observation.get("event_ms", 0)))
        alias = decode(observation.get("alias") or observation.get("metadata", {}).get("user_api_key_alias", ""))
        if isinstance(alias, str) and alias.strip():
            aliases.add(alias.strip())
        call_kind = call_kinds.get(index)
        if call_kind in ("title", "auxiliary"):
            continue
        branch = branch_kinds.get(index)
        history = histories.setdefault(branch, [])
        if inp is not None and not (isinstance(inp, list) or isinstance(inp, dict) and
                                    ("messages" in inp or "content" in inp or "tool_calls" in inp)):
            warnings.add("unrecognized_input_schema")
        snapshot = normalize(inp, observation, "input")
        classify_client_history(snapshot, inp, literal_replies)
        if call_kind == "compaction":
            transformed = []
            for event in snapshot:
                span = compaction_span(event["content"]) if event["kind"] == "text" else None
                if span:
                    start, end = span
                    base = event.get("source_span", [0])[0]
                    if event["content"][:start].strip():
                        transformed.append(dict(event, content=event["content"][:start], source_span=[base, base + start]))
                    event = dict(event, content=event["content"][start:end], source_span=[base + start, base + end],
                                 message_id=hashlib.sha256((event["message_id"] + ":compaction").encode()).hexdigest()[:24],
                                 kind="compaction_request", classification_reason="runtime_compaction_template")
                transformed.append(event)
            snapshot = transformed
            warnings.add("compacted_context")
        if any(m["kind"] == "context_summary" for m in snapshot):
            warnings.add("compacted_context")
            if not history:
                warnings.add("missing_pre_compaction_history")
        history = merge_snapshot(history, snapshot, warnings)
        # This call's newly generated output keeps this observation as its source.
        if str(observation.get("level", "")).upper() == "ERROR":
            history.append(call_error(observation, output))
        else:
            generated = normalize(output, observation, "output")
            if call_kind == "compaction":
                for event in generated:
                    if event["kind"] == "text":
                        event.update(kind="context_summary", classification_reason="runtime_compaction_template")
            if any(event["kind"] == "text" and event["content"].strip() == "No response requested." for event in generated):
                literal_replies.add(input_signature(inp))
            history.extend(generated)
        histories[branch] = history
    expanded = set()
    def expand(branch, visited):
        expanded.add(branch)
        result = []
        for event in histories.get(branch, []):
            if branch:
                event.update(branch_id=branch, branch_tool=branch_tools.get(branch, "Agent"))
            result.append(event)
            child = event.get("tool_call_id")
            if event["kind"] in ("tool_call", "confirmation_request") and child in histories and child not in visited:
                result.extend(expand(child, visited | {child}))
        return result
    history = expand(None, set())
    for branch in histories:
        if branch is not None and branch not in expanded:
            warnings.add("unlinked_subagent_context")
            history.extend(expand(branch, {branch}))
    requests = {}
    for event in history:
        call_id = event.get("tool_call_id")
        if event["kind"] == "confirmation_request" and call_id:
            requests[call_id] = event
        elif event["kind"] == "tool_result" and call_id in requests:
            request = requests[call_id]
            content = event["content"].lstrip()
            validation_failed = content.startswith(("<tool_use_error>InputValidationError:", "InputValidationError:"))
            user_rejected = content.startswith(("The user doesn't want to proceed with this tool use.",
                                               "User rejected", "User declined", "User denied", "User cancelled"))
            if event.get("is_error") and validation_failed:
                request.update(kind="tool_call", status="failed_validation", classification_reason="confirmation_validation_failed")
            elif not event.get("is_error") or user_rejected:
                event.update(role="user", kind="confirmation_answer", reply_to=request["message_id"])
                if user_rejected:
                    event["decision"] = "rejected"
            else:
                warnings.add("unresolved_confirmation")
    for event in history:
        if event.get("branch_id") and event["kind"] not in ("confirmation_request", "confirmation_answer") and event.get("classification_reason") != "client_mid_turn_user_message":
            event.update(original_kind=event["kind"], kind="subagent_context", classification_reason="explicit_delegated_tool_call")
    for index, event in enumerate(history):
        event["sequence"] = index
    if len(aliases) > 1:
        warnings.add("multiple_aliases")
    if any(m["kind"] == "context_summary" for m in history) and not any(
            m["role"] == "assistant" and m["kind"] == "text" for m in history):
        warnings.update(("missing_pre_compaction_history", "missing_assistant_history"))
    title = next((m["content"].strip()[:80] for m in history
                  if m["role"] == "user" and m["kind"] == "text"), trace)
    count = len(dialogue_messages(history))
    return dict(project_id=project, trace_id=trace, aliases=sorted(aliases), title=title,
                first_ms=first_ms or 0, last_ms=last_ms, event_ms=event_ms,
                observation_count=len(ids), message_count=count, warnings=sorted(warnings),
                messages=history, observation_ids=ids)
