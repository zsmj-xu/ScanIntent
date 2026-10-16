import copy
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from clickhouse import Client, array_parameter
from collector import WINDOW_MS, collect
from reconstruction import dialogue_messages, reconstruct
from scanintent import parse_time
from source import Source
from store import Store
from validate_trace import prepare_case
from web import conversation_page


def message(role, content):
    return dict(role=role, content=content)


def observation(oid, messages, output=None, **kwargs):
    return dict(observation_id=oid, project_id="p", trace_id="t", start_ms=1,
                event_ms=2, alias="ht-cbm-server", input={"messages": messages}, output=output,
                **kwargs)


class ViewerTests(unittest.TestCase):
    def test_message_pages_preserve_order_sources_and_hide_internal_records(self):
        messages = [dict(message_id=str(i), kind="text", role="user", content=str(i), observation_id=str(i),
                         field="input", path=f"/messages/{i}/content", source_span=[0, 2]) for i in range(45)]
        messages.insert(1, dict(kind="tool_result", role="tool", content="internal", observation_id="tool"))
        row = dict(trace_id="test", messages=messages, observation_ids=["one", "two"])
        page = conversation_page(row, 2)
        self.assertEqual(page["record_total"], 45)
        self.assertEqual([m["content"] for m in page["records"]], [str(i) for i in range(40, 45)])
        self.assertEqual(page["records"][0]["observation_id"], "40")
        self.assertEqual(page["records"][0]["parts"][0]["source_span"], [0, 2])
        self.assertNotIn("messages", page)
        self.assertNotIn("observation_ids", page)
        self.assertEqual(page["observation_id_count"], 2)
        self.assertEqual(conversation_page(row, 999)["message_page"], 2)
        self.assertEqual(conversation_page(row, 0)["message_page"], 1)
        all_records = conversation_page(row, 1, internal=True)
        self.assertEqual(all_records["record_total"], 46)
        self.assertEqual(all_records["records"][1]["kind"], "tool_result")
        self.assertEqual(len(row["messages"]), 46)

    def test_empty_conversation_has_one_empty_page(self):
        page = conversation_page(dict(messages=[], observation_ids=[]), 999)
        self.assertEqual((page["message_page"], page["record_total"], page["records"]), (1, 0, []))
        with self.assertRaises(ValueError):
            conversation_page(dict(messages=[]), "invalid")


class ReconstructionTests(unittest.TestCase):
    def test_dialogue_preserves_source_text_without_redaction(self):
        body = '示例 password="fixture-not-a-real-password"\n目录 PWD=/example/workspace\n说明中再次引用 fixture-not-a-real-password'
        result = reconstruct("p", "t", [observation("one", [message("user", "核对原文")], message("assistant", body))])
        self.assertEqual(dialogue_messages(result["messages"])[-1]["content"], body)
        self.assertFalse(any("redactions" in event for event in result["messages"]))

    def test_policy_review_variants_do_not_replay_transcript_as_dialogue(self):
        for suffix in ("Err on the side of blocking. Stage 1 does NOT apply user intent. <block> immediately.",
                       "Review the classification process and follow it carefully. Use <thinking> before <block>."):
            policy = observation("policy", [message("user", "<transcript>用户和工具的回放</transcript>\n" + suffix)], message("assistant", "<block />"))
            result = reconstruct("p", "t", [policy, observation("main", [message("user", "实际问题")], message("assistant", "实际回答"))])
            self.assertEqual([m["content"] for m in dialogue_messages(result["messages"])], ["实际问题", "实际回答"])
        genuine = reconstruct("p", "t", [observation("main", [message("user", "<transcript>文本</transcript>请总结")], message("assistant", "总结"))])
        self.assertEqual(genuine["message_count"], 2)

    def test_fenced_title_and_session_wrapper_require_main_prompt_reuse(self):
        schema = json.dumps(dict(type="object", properties={"title": {"type": "string"}}, required=["title"]))
        inp = message("user", [dict(type="text", text="<session>真实需求</session>"), dict(type="text", text=schema)])
        result = reconstruct("p", "t", [observation("title", [inp], message("assistant", '```json\n{"title":"标题"}\n```')),
            observation("main", [message("user", "真实需求"), message("assistant", "第一步")], message("assistant", "完成"))])
        self.assertEqual(result["message_count"], 3)
        self.assertFalse(any(m["observation_id"] == "title" for m in result["messages"]))

    def test_system_wrapped_mid_turn_user_messages_are_not_lost(self):
        for suffix in ("\n\nIMPORTANT: After completing your current task, you MUST address the user's message above. Do not ignore it.",
                       "\n\nThis is how Claude Code surfaces messages the user sends mid-turn — alongside the next tool result."):
            wrapped = "The user sent a new message while you were working:\n真正的插话" + suffix
            rows = [message("user", "原问题"), message("assistant", "正在处理"), message("system", wrapped)]
            result = reconstruct("p", "t", [observation("one", rows, message("assistant", "回答插话")),
                observation("two", rows + [message("assistant", "回答插话")], message("assistant", "结束"))])
            users = [m["content"] for m in dialogue_messages(result["messages"]) if m["role"] == "user"]
            self.assertEqual(users, ["原问题", "真正的插话"])
            e = next(e for e in result["messages"] if e.get("classification_reason") == "client_mid_turn_user_message")
            self.assertEqual(wrapped[slice(*e["source_span"])], e["content"])

    def test_agent_and_search_branches_preserve_evidence_without_fake_user_turns(self):
        for name, args, prompt in (("Agent", {"prompt":"独立子任务"}, "独立子任务"),
                                   ("WebSearch", {"query":"目标查询"}, "Perform a web search for the query: 目标查询")):
            call = dict(role="assistant", content="", tool_calls=[dict(id="delegate", function=dict(name=name, arguments=json.dumps(args)))])
            user = message("user", "主问题")
            result = reconstruct("p", "t", [observation("parent", [user], call),
                observation("child", [message("user", prompt)], message("assistant", "子任务答案")),
                observation("return", [user, call, dict(role="tool", tool_call_id="delegate", content="子任务答案")], message("assistant", "主回复"))])
            self.assertEqual([m["content"] for m in dialogue_messages(result["messages"])], ["主问题", "主回复"])
            self.assertTrue(any(m["kind"] == "subagent_context" and m["content"] == "子任务答案" for m in result["messages"]))

    def test_context_wrappers_and_cancel_markers_keep_neighboring_real_input(self):
        context = "# AGENTS.md instructions for /workspace\n<INSTRUCTIONS>规则</INSTRUCTIONS>"
        rows = [message("user", context), message("user", "<environment_context>环境</environment_context>真正问题"),
                message("user", [dict(type="text", text="[Request interrupted by user]"), dict(type="text", text="怎么停了")]),
                message("user", "<task-notification>任务完成</task-notification>请继续")]
        result = reconstruct("p", "t", [observation("one", rows)])
        self.assertEqual([m["content"] for m in dialogue_messages(result["messages"])], ["真正问题", "怎么停了", "请继续"])

    def test_compaction_appended_to_user_text_preserves_prefix_and_ignores_trailing_system(self):
        request = "CRITICAL: Respond with TEXT ONLY. Your task is to create a detailed summary of the conversation so far. <analysis> <summary> Tool calls will be REJECTED"
        rows = [message("user", "解释一下\n\n" + request), message("system", "hook context")]
        result = reconstruct("p", "t", [observation("compact", rows, None),
            observation("retry", rows, message("assistant", "<analysis>内部</analysis><summary>摘要</summary>")),
            observation("main", [message("user", "解释一下\n")], message("assistant", "实际解释"))])
        self.assertEqual([m["content"].strip() for m in dialogue_messages(result["messages"])], ["解释一下", "实际解释"])




    def test_compacted_snapshot_with_no_assistant_prose_reports_missing_history(self):
        result = reconstruct("p", "t", [observation("one", [message("user", "问题"),
            message("user", "Another language model started to solve this problem and produced a summary of its thinking process.")])])
        self.assertEqual(result["message_count"], 1)
        self.assertIn("missing_assistant_history", result["warnings"])

    def test_title_schema_requires_corroborating_main_history(self):
        schema = json.dumps(dict(type="object", properties={"title": {"type": "string"}}, required=["title"]))
        prompt = message("user", [dict(type="text", text="创建插件"), dict(type="text", text=schema)])
        helper = observation("title", [prompt], message("assistant", '{"title":"插件导出"}'))
        main = observation("main", [message("user", "创建插件\n"), message("assistant", "开始")], message("assistant", "完成"))
        result = reconstruct("p", "t", [helper, main])
        self.assertEqual([m["content"] for m in result["messages"]], ["创建插件\n", "开始", "完成"])
        self.assertEqual(result["observation_ids"], ["title", "main"])
        actual_title_request = reconstruct("p", "t", [helper])
        self.assertTrue(any(m["content"] == '{"title":"插件导出"}' for m in actual_title_request["messages"]))

    def test_compaction_preserves_tool_result_including_last_or_empty_attempt(self):
        request = "CRITICAL: Respond with TEXT ONLY. Your task is to create a detailed summary of the conversation so far. <analysis> <summary> Tool calls will be REJECTED"
        user = message("user", "需求")
        assistant = message("assistant", "旧回复")
        final_input = message("user", [dict(type="tool_result", tool_use_id="call", content="实际执行结果"), dict(type="text", text=request)])
        compact = observation("compact", [user, assistant, final_input], message("assistant", "<analysis>过程</analysis><summary>上下文摘要</summary>"))
        after = observation("after", [message("user", "This session is being continued from a previous conversation. 上下文摘要")], message("assistant", "继续执行"))
        result = reconstruct("p", "t", [observation("one", [user], assistant), compact, after])
        self.assertEqual([m["content"] for m in result["messages"] if m["kind"] == "text"], ["需求", "旧回复", "继续执行"])
        self.assertTrue(any(m["content"] == "实际执行结果" for m in result["messages"]))
        self.assertEqual(sum(m["kind"] == "compaction_request" for m in result["messages"]), 1)
        ordinary = reconstruct("p", "t", [observation("one", [user], assistant), compact])
        self.assertTrue(any(m["kind"] == "compaction_request" for m in ordinary["messages"]))
        real_request = "Please summarize our conversation, including what remains to do."
        real = reconstruct("p", "t", [observation("one", [message("user", real_request)], message("assistant", "<summary>Here is the summary</summary>"))])
        self.assertEqual(real["message_count"], 2)
        self.assertEqual(real["messages"][0]["kind"], "text")

    def test_generation_failure_is_internal_instead_of_an_assistant_reply(self):
        bad = observation("bad", [message("user", "开始")], message("assistant", "litellm.Timeout: secret-token"),
                          level="ERROR", status_message="litellm.Timeout: secret-token")
        good = observation("good", [message("user", "开始")], message("assistant", "完成"))
        result = reconstruct("p", "t", [bad, good])
        self.assertEqual([m["content"] for m in dialogue_messages(result["messages"])], ["开始", "完成"])
        error = next(m for m in result["messages"] if m["kind"] == "call_error")
        self.assertIn("litellm.Timeout", error["content"])
        self.assertNotIn("secret-token", json.dumps(result))

    def test_invalid_confirmation_is_internal_but_real_rejection_is_user_answer(self):
        ask = dict(role="assistant", content="", tool_calls=[dict(id="ask", function=dict(name="AskUserQuestion", arguments='{"questions":[]}'))])
        user = message("user", "开始")
        failed = message("user", [dict(type="tool_result", tool_use_id="ask", is_error=True, content="<tool_use_error>InputValidationError: bad options</tool_use_error>")])
        result = reconstruct("p", "t", [observation("one", [user], ask), observation("two", [user, ask, failed])])
        self.assertFalse(any(m["kind"] == "confirmation_request" for m in result["messages"]))
        rejected = message("user", [dict(type="tool_result", tool_use_id="ask", is_error=True, content="The user doesn't want to proceed with this tool use. Please change the plan.")])
        result = reconstruct("p", "t", [observation("one", [user], ask), observation("two", [user, ask, rejected])])
        reply = next(m for m in result["messages"] if m["kind"] == "confirmation_answer")
        self.assertEqual((reply["role"], reply["decision"]), ("user", "rejected"))

    def test_count_groups_blocks_but_not_different_original_messages(self):
        rows = [message("user", [dict(type="text", text="第一段"), dict(type="text", text="第二段")]), message("user", "另一条")]
        result = reconstruct("p", "t", [observation("one", rows, message("assistant", "回答"))])
        grouped = dialogue_messages(result["messages"])
        self.assertEqual(result["message_count"], 3)
        self.assertEqual(len(grouped[0]["parts"]), 2)
        self.assertEqual([p["path"] for p in grouped[0]["parts"]], ["/messages/0/content/0/text", "/messages/0/content/1/text"])

    def test_upload_manifest_becomes_attachments_in_original_user_message(self):
        manifest = dict(resources=[dict(fileName="a.png", contentType="image/png", url="https://secret.invalid/token", podFilePath="/private/path")])
        text = "请检查图片\n<hetao-cloudcli-user-resources>" + json.dumps(manifest) + "</hetao-cloudcli-user-resources>"
        result = reconstruct("p", "t", [observation("one", [message("user", text)])])
        self.assertEqual(result["message_count"], 1)
        parts = dialogue_messages(result["messages"])[0]["parts"]
        self.assertEqual([p["kind"] for p in parts], ["text", "attachment"])
        self.assertEqual(parts[1]["content"], "a.png")
        self.assertEqual(parts[1]["manifest_file_index"], 0)
        self.assertNotIn("secret.invalid", json.dumps(result))
        self.assertNotIn("/private/path", json.dumps(result))

    def test_image_tool_annotation_is_context_and_real_user_text_survives(self):
        call = message("assistant", [dict(type="tool_use", id="read", name="Read", input={"file_path":"a.png"})])
        result_block = dict(type="tool_result", tool_use_id="read", content=[dict(type="image", source={"type":"base64", "data":"media-reference"})])
        note = "[Image: original 2560x2560, displayed at 2000x2000. Multiply coordinates by 1.28.]"
        mixed = message("user", [result_block, dict(type="text", text=note + "\n请比较这张图")])
        result = reconstruct("p", "t", [observation("one", [message("user", "读图"), call, mixed])])
        dialogue = dialogue_messages(result["messages"])
        self.assertEqual([m["content"] for m in dialogue], ["读图", "\n请比较这张图"])
        self.assertTrue(any(m["kind"] == "tool_result" and "[工具返回图片]" in m["content"] for m in result["messages"]))
        self.assertTrue(any(m.get("classification_reason") == "image_tool_description" for m in result["messages"]))
        genuine = reconstruct("p", "t", [observation("one", [message("user", note)])])
        self.assertEqual(genuine["message_count"], 1)

    def test_client_controls_preserved_internally_and_genuine_literals_stay_visible(self):
        initial = message("user", [dict(type="text", text="初始问题"), dict(type="text", text="Continue from where you left off.")])
        placeholder = message("assistant", "No response requested.")
        question = message("user", "请原样回复 No response requested.")
        first = observation("one", [initial, placeholder, question], placeholder)
        second = observation("two", [initial, placeholder, question, placeholder, message("user", "真实后续")], message("assistant", "完成"))
        result = reconstruct("p", "t", [first, second])
        dialogue = dialogue_messages(result["messages"])
        self.assertEqual(sum(m["content"] == "No response requested." for m in dialogue), 1)
        self.assertFalse(any(m["content"] == "Continue from where you left off." for m in dialogue))
        genuine_continue = reconstruct("p", "t", [observation("one", [message("user", "Continue from where you left off.")], message("assistant", "继续执行"))])
        self.assertEqual(genuine_continue["message_count"], 2)

    def test_repeated_history_preserves_first_source_and_real_repetitions(self):
        user, answer = message("user", "继续"), message("assistant", "好的")
        result = reconstruct("p", "t", [observation("one", [user], answer),
                                       observation("two", [user, answer, user], answer)])
        self.assertEqual([m["content"] for m in result["messages"]], ["继续", "好的", "继续", "好的"])
        self.assertEqual([m["observation_id"] for m in result["messages"]], ["one", "one", "two", "two"])
        self.assertEqual(result["aliases"], ["ht-cbm-server"])
        self.assertFalse(result["warnings"])

    def test_confirmation_tool_result_is_displayed_between_question_and_continuation(self):
        call = dict(role="assistant", content="请确认", tool_calls=[
            dict(id="confirm", function=dict(name="functions.request_user_input", arguments='{"questions":[{"title":"继续吗"}]}'))])
        history_call = dict(role="assistant", content=[dict(type="text", text="请确认"),
            dict(type="tool_use", id="confirm", name="functions.request_user_input",
                 input={"questions": [{"title": "继续吗"}]})])
        user = message("user", "开始")
        tool = message("user", [dict(type="tool_result", tool_use_id="confirm", content="同意")])
        result = reconstruct("p", "t", [observation("one", [user], call),
            observation("two", [user, history_call, tool], message("assistant", "继续执行"))])
        self.assertEqual([m["kind"] for m in result["messages"]],
                         ["text", "text", "confirmation_request", "confirmation_answer", "text"])
        reply = result["messages"][-2]
        self.assertEqual((reply["role"], reply["observation_id"]), ("user", "two"))
        self.assertEqual(reply["reply_to"], result["messages"][2]["message_id"])

    def test_same_tool_id_deduplicates_provider_argument_rewrites(self):
        first = dict(role="assistant", content="", tool_calls=[dict(id="call", function=dict(
            name="Bash", arguments='{"command":"pwd","description":"inspect"}'))])
        next_input = message("assistant", [dict(type="tool_use", id="call", name="Bash", input={"command":"pwd"})])
        result = reconstruct("p", "t", [observation("one", [message("user", "inspect")], first),
            observation("two", [message("user", "inspect"), next_input], message("assistant", "done"))])
        self.assertEqual(sum(m["kind"] == "tool_call" for m in result["messages"]), 1)
        self.assertFalse(result["warnings"])

    def test_compaction_keeps_prior_history_and_original_provenance(self):
        summary = message("user", "This session is being continued from a previous conversation that ran out of context. Summary...")
        result = reconstruct("p", "t", [observation("one", [message("user", "需求")], message("assistant", "旧回复")),
            observation("two", [summary, message("user", "新问题")], message("assistant", "新回复"))])
        self.assertEqual([m["content"] for m in result["messages"] if m["kind"] == "text"],
                         ["需求", "旧回复", "新问题", "新回复"])
        self.assertIn("compacted_context", result["warnings"])
        self.assertNotIn("missing_pre_compaction_history", result["warnings"])
        partial = reconstruct("p", "t", [observation("two", [summary])])
        self.assertIn("missing_pre_compaction_history", partial["warnings"])

    def test_tool_and_injected_context_do_not_turn_into_user_messages(self):
        inp = [message("user", [dict(type="text", text="<system-reminder>internal</system-reminder>真实提问"),
                               dict(type="tool_result", tool_use_id="machine", content="执行结果")])]
        result = reconstruct("p", "t", [observation("one", inp)])
        self.assertEqual([m["kind"] for m in result["messages"]], ["context", "text", "tool_result"])
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(result["title"], "真实提问")
        self.assertEqual(result["messages"][1]["path"], "/messages/0/content/0/text")

    def test_unknown_sequence_is_preserved_with_warning_and_alias_does_not_merge_traces(self):
        result = reconstruct("p", "t", [observation("one", [message("user", "a")]),
                                        observation("two", [message("user", "b")])])
        self.assertIn("unlinked_snapshot", result["warnings"])
        self.assertEqual(len(result["messages"]), 2)
        with self.assertRaisesRegex(ValueError, "Mixed trace"):
            reconstruct("p", "other", [observation("one", [])])

    def test_local_verified_samples_retain_all_clean_messages(self):
        paths = list(Path(__file__).parent.glob("*.conversation.json"))
        if not paths:
            self.skipTest("Private local sample files are not distributed")
        for path in paths:
            raw = json.loads(path.read_text())
            result = reconstruct(raw["project_id"], raw["trace_id"], raw["observations"])
            baseline = json.loads(path.with_name(path.stem + ".clean.json").read_text())
            for expected in baseline:
                self.assertTrue(any(m["role"] == expected["role"] and
                    m["content"].strip() == expected["content"].strip() for m in result["messages"]))


class FakeSource:
    def clock(self, timings):
        return dict(server_ms=100000000, timezone="Asia/Shanghai")

    def discover(self, project, start, end, timings, **kwargs):
        self.window = start, end
        yield dict(trace_id="t")

    def observations(self, project, traces, timings):
        yield observation("one", [message("user", "hello")])


class FakeStore:
    def __init__(self):
        self.saved, self.logs = {}, []

    def checkpoint(self, project, scope):
        return max((r["window_end_ms"] for r in self.logs if r["status"] == "success"), default=None)

    def save_many(self, conversations):
        for row in conversations:
            self.saved[row["trace_id"]] = copy.deepcopy(row)

    def save_run(self, run):
        self.logs.append(copy.deepcopy(run))


class CollectionTests(unittest.TestCase):
    def test_test_sample_does_not_claim_whole_window_success(self):
        source, store = FakeSource(), FakeStore()
        with patch.object(source, "discover", return_value=iter([dict(trace_id="t"), dict(trace_id="other")])):
            run = collect(source, store, "p", start_ms=1000, max_traces=1)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["trace_count"], 1)
        self.assertIsNone(store.checkpoint("p", "scope"))

    def test_one_window_idempotent_retry_and_resume(self):
        source, store = FakeSource(), FakeStore()
        run = collect(source, store, "p", start_ms=1000)
        self.assertEqual(source.window, (1000, 1000 + WINDOW_MS))
        self.assertEqual(run["status"], "success")
        self.assertEqual(len(store.saved), 1)
        collect(source, store, "p", start_ms=1000)
        self.assertEqual(len(store.saved), 1)
        collect(source, store, "p", resume=True)
        self.assertEqual(source.window[0], 1000 + WINDOW_MS)
        self.assertTrue(all(k in json.loads(run["timings_json"]) for k in
                            ("clock_ms", "discovery_ms", "history_ms", "parse_ms", "reconstruct_ms", "persist_ms", "total_ms")))

    def test_failure_does_not_advance_checkpoint(self):
        source, store = FakeSource(), FakeStore()
        with patch.object(store, "save_many", side_effect=RuntimeError("private body")):
            with self.assertRaisesRegex(RuntimeError, "window not advanced"):
                collect(source, store, "p", start_ms=1000)
        self.assertIsNone(store.checkpoint("p", "scope"))
        self.assertEqual(store.logs[-1]["status"], "failed")
        self.assertEqual(store.logs[-1]["trace_count"], 0)
        self.assertNotIn("private", json.dumps(store.logs))

    def test_discovery_failure_still_records_failure_and_partial_timings(self):
        source, store = FakeSource(), FakeStore()
        with patch.object(source, "discover", side_effect=RuntimeError("private")):
            with self.assertRaises(RuntimeError):
                collect(source, store, "p", start_ms=1000)
        self.assertGreater(json.loads(store.logs[-1]["timings_json"])["total_ms"], 0)
        self.assertEqual(store.logs[-1]["status"], "failed")

    def test_future_or_timezone_less_window_rejected(self):
        with self.assertRaises(ValueError):
            collect(FakeSource(), FakeStore(), "p", start_ms=99999999)
        with self.assertRaises(ValueError):
            parse_time("2026-10-15T12:00:00")
        self.assertEqual(parse_time("2026-10-15T12:00:00+08:00"), parse_time("2026-10-15T04:00:00Z"))

    def test_missing_full_trace_fails_instead_of_advancing(self):
        source, store = FakeSource(), FakeStore()
        with patch.object(source, "observations", return_value=iter([])):
            with self.assertRaises(RuntimeError):
                collect(source, store, "p", start_ms=1000)
        self.assertEqual(store.logs[-1]["status"], "failed")

    def test_discovery_uses_ingest_window_and_history_has_no_window_filter(self):
        client = unittest.mock.Mock()
        client.rows.return_value = []
        source = Source(client)
        source.discover("p", 1000, 1000 + WINDOW_MS, {}, alias="a")
        sql, params = client.rows.call_args.args
        self.assertIn("event_ts >=", sql)
        self.assertIn("event_ts <", sql)
        self.assertNotIn("start_time >=", sql)
        client.rows.side_effect = [[dict(trace_id="t", id="one", type="GENERATION", day="2026-10-15")],
                                  [observation("one", [])]]
        list(source.observations("p", ["t"], {}))
        self.assertNotIn("start_time >=", client.rows.call_args.args[0])
        self.assertIn("FINAL", client.rows.call_args.args[0])
        self.assertIn("PREWHERE", client.rows.call_args.args[0])
        self.assertEqual(client.rows.call_args.args[1]["day"], "2026-10-15")

    def test_body_requests_batch_multiple_traces_then_sort_chronologically(self):
        client = unittest.mock.Mock()
        refs = [dict(trace_id=trace, id=oid, type="GENERATION", day="2026-10-15")
                for trace, oid in (("t", "one"), ("u", "two"))]
        first, second = observation("one", []), observation("two", [])
        second["trace_id"] = "u"
        client.rows.side_effect = [refs, [second, first]]
        result = list(Source(client).observations("p", ["t", "u"], {}))
        self.assertEqual([r["trace_id"] for r in result], ["t", "u"])
        self.assertEqual(client.rows.call_count, 2)
        self.assertIn("one", client.rows.call_args.args[1]["ids"])
        self.assertIn("two", client.rows.call_args.args[1]["ids"])


class TransportTests(unittest.TestCase):
    @patch("clickhouse.urlopen")
    def test_streaming_failure_does_not_become_success_or_disclose_body(self, open_url):
        response = open_url.return_value
        response.__enter__.return_value = response
        response.readline.side_effect = [b'{"trace_id":"one"}\n', b'{"exception":"private SQL"}\n']
        client = Client(("http://localhost/", "d", "Basic secret"), readonly=True)
        rows = client.rows("SELECT 1")
        self.assertEqual(next(rows), dict(trace_id="one"))
        with self.assertRaisesRegex(RuntimeError, "query failed"):
            next(rows)
        url = open_url.call_args.args[0].full_url
        self.assertNotIn("secret", url)
        self.assertIn("readonly=1", url)

    def test_source_rejects_writes_and_array_ids_escape(self):
        client = Client(("http://localhost/", "d", "Basic test"), readonly=True)
        with self.assertRaises(ValueError):
            client.execute("CREATE TABLE x")
        self.assertEqual(array_parameter(["a'\\b"]), "['a\\'\\\\b']")


class ReviewEvidenceTests(unittest.TestCase):
    def test_review_case_is_private_immutable_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            raw.write_text(json.dumps([observation("one", [message("user", "hello")], message("assistant", "reply"))]))
            case = root / "round-1"
            manifest = prepare_case("p", "t", raw, case)
            self.assertEqual(manifest["status"], "pending_independent_review")
            self.assertEqual(manifest["message_count"], 2)
            self.assertEqual(case.stat().st_mode & 0o777, 0o700)
            self.assertEqual((case / "candidate.json").stat().st_mode & 0o777, 0o600)
            self.assertIn("dialogue.json", manifest["artifact_sha256"])
            with self.assertRaises(FileExistsError):
                prepare_case("p", "t", raw, case)

    def test_mixed_trace_review_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            raw.write_text(json.dumps([observation("one", [])]))
            with self.assertRaises(ValueError):
                prepare_case("p", "different", raw, root / "review")
            self.assertFalse((root / "review").exists())


@unittest.skipUnless(os.environ.get("SCANINTENT_TEST_CK") == "1", "Requires dedicated local CK")
class ClickHouseIntegrationTests(unittest.TestCase):
    def test_browse_filters_counts_and_stable_pages(self):
        store = Store(Client.target())
        project = "test-" + uuid.uuid4().hex
        try:
            for trace, stamp, title, alias, warnings in (
                ("a", 1000, "早期会话", "alpha", []),
                ("b", 2000, "Search target", "alpha", ["compacted_context"]),
                ("c", 2000, "普通会话", "beta", []),
            ):
                source = observation("one", [message("user", title)])
                source["trace_id"] = trace
                row = reconstruct(project, trace, [source])
                row.update(last_ms=stamp, title=title, aliases=[alias], warnings=warnings)
                store.save(row)
                if trace == "b":
                    store.save(row)  # Replaced versions must not inflate totals or pages.
            first = store.browse(project, size=2)
            self.assertEqual((first["total"], first["page"]), (3, 1))
            self.assertEqual([r["trace_id"] for r in first["conversations"]], ["c", "b"])
            self.assertEqual([r["trace_id"] for r in store.browse(project, page=2, size=2)["conversations"]], ["a"])
            self.assertEqual(store.browse(project, page=999, size=2)["page"], 2)
            self.assertEqual(store.browse(project, alias="alpha")["total"], 2)
            self.assertEqual(store.browse(project, query="search")["total"], 1)
            self.assertEqual(store.browse(project, query="普通")["total"], 1)
            self.assertEqual(store.browse(project, query="c")["total"], 2)
            self.assertEqual(store.browse(project, warning=True, alias="beta")["total"], 0)
            self.assertEqual(store.browse(project, warning=True)["total"], 1)
            self.assertEqual(store.browse(project, start=1000, end=2000)["total"], 1)
            self.assertEqual(store.browse(project, query="' OR 1=1 --")["total"], 0)
            self.assertEqual(store.aliases(project), ["alpha", "beta"])
            self.assertEqual(store.browse(project, order="oldest")["conversations"][0]["trace_id"], "a")
            with self.assertRaises(ValueError):
                store.browse(project, start=2000, end=1000)
        finally:
            store.client.execute("ALTER TABLE conversations DELETE WHERE project_id='" + project + "' SETTINGS mutations_sync=1")

    def test_snapshot_replacement_pagination_and_run_checkpoint(self):
        store = Store(Client.target())
        store.initialize()
        project = "test-" + uuid.uuid4().hex
        try:
            a = reconstruct(project, "t", [observation("one", [message("user", "test")])])
            store.save(a)
            a["messages"] = []
            a["message_count"] = 0
            store.save(a)
            self.assertEqual(store.get(project, "t")["messages"], [])
            self.assertEqual(len(store.list(project, alias="ht-cbm-server")), 1)
            self.assertEqual(store.list(project, before_ms=1, before_trace="t"), [])
            run = collect(FakeSource(), store, project, start_ms=1000)
            self.assertEqual(store.checkpoint(project, run["scope"]), 1000 + WINDOW_MS)
            self.assertEqual(len(store.runs(project)), 1)
        finally:
            # The namespace is generated above and contains only this test's rows.
            for table in ("conversations", "collection_runs"):
                store.client.execute("ALTER TABLE " + table + " DELETE WHERE project_id='" + project + "' SETTINGS mutations_sync=1")


if __name__ == "__main__":
    unittest.main()
