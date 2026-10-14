from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from providers import collaborative_agent


class CollaborativeAgentTests(unittest.TestCase):
    def test_lightweight_router_has_low_effort_short_budget_and_no_transport_retries(self):
        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"route":"conversation","participants":[],"effort":"low"}'),
        )])
        with patch.object(collaborative_agent, "openai_client", client), \
                patch.object(collaborative_agent, "LUNA_BACKEND", "direct"):
            result = collaborative_agent.query_room_route("烧卖你们觉得好吃吗？", "Route ordinary room conversation.")
        self.assertIn('"conversation"', result)
        client.with_options.assert_called_once_with(timeout=12.0, max_retries=0)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["max_completion_tokens"], 2048)

    def test_brief_primary_reply_uses_recent_context_without_full_history_or_compression(self):
        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="看着很香！"), finish_reason="stop",
        )])
        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "DEEPSEEK_BACKEND", "direct"), \
                patch.object(collaborative_agent, "_load_history") as full_history, \
                patch.object(collaborative_agent, "conversation_context", return_value="User shared homemade 烧卖."):
            answer = collaborative_agent.query_agent(
                "觉得好吃吗？", "room", None, persist=False, effort="low", response_depth="concise", request_timeout=35,
            )
        full_history.assert_not_called()
        client.with_options.assert_called_once_with(timeout=35, max_retries=0)
        self.assertEqual(answer, "看着很香！")
        request = client.chat.completions.create.call_args.kwargs
        self.assertIn("homemade 烧卖", str(request["messages"]))
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["max_tokens"], 1000)

    def test_native_gemini_conversation_uses_low_effort_and_bounded_request(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="给我留一颗！", candidates=[])
        with patch.object(collaborative_agent, "gemini_client", client), \
                patch.object(collaborative_agent, "GEMINI_BACKEND", "direct"), \
                patch.object(collaborative_agent, "_gemini_request_slot"):
            answer = collaborative_agent.consult_gemini("Reply to the food photo.", response_depth="concise",
                                                        effort="low", request_timeout=35)
        self.assertEqual(answer, "给我留一颗！")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.thinking_config.thinking_level, collaborative_agent.GEMINI_THINKING_LEVELS["low"])
        self.assertEqual(config.max_output_tokens, 1000)
        self.assertEqual(config.http_options.timeout, 35000)
        self.assertEqual(config.http_options.retry_options.attempts, 1)

    def test_native_claude_conversation_uses_low_adaptive_effort_and_no_transport_retries(self):
        client = Mock()
        client.with_options.return_value = client
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="摆盘也挺整齐。")], stop_reason="end_turn",
        )
        with patch.object(collaborative_agent, "claude_client", client), \
                patch.object(collaborative_agent, "CLAUDE_BACKEND", "direct"):
            answer = collaborative_agent.consult_claude("Reply to the food photo.", response_depth="concise",
                                                        effort="low", request_timeout=35)
        self.assertEqual(answer, "摆盘也挺整齐。")
        client.with_options.assert_called_once_with(timeout=35, max_retries=0)
        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["thinking"], {"type": "adaptive"})
        self.assertEqual(request["output_config"], {"effort": "low"})
        self.assertEqual(request["max_tokens"], 2024)

    def test_clear_history_removes_attachment_index_and_cache(self):
        with patch.object(collaborative_agent.db, "clear_chat_history") as clear_history, \
                patch.object(collaborative_agent.db, "clear_conversation_attachments") as clear_attachments, \
                patch("services.attachment_store.clear_conversation_cache") as clear_cache:
            collaborative_agent.clear_history("channel")

        clear_history.assert_called_once_with("channel", "agent")
        clear_attachments.assert_called_once_with("channel")
        clear_cache.assert_called_once_with("channel")

    def test_gemini_request_slot_waits_for_configured_interval(self):
        previous_request_at = collaborative_agent._last_gemini_request_at
        collaborative_agent._last_gemini_request_at = 10.0
        try:
            with patch.object(collaborative_agent, "GEMINI_MIN_INTERVAL_SECONDS", 1.1), \
                    patch.object(collaborative_agent.time, "monotonic", side_effect=[10.5, 11.1]), \
                    patch.object(collaborative_agent.time, "sleep") as sleep:
                with collaborative_agent._gemini_request_slot():
                    pass

            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], 0.6)
            self.assertEqual(collaborative_agent._last_gemini_request_at, 11.1)
        finally:
            collaborative_agent._last_gemini_request_at = previous_request_at

    def test_existing_agent_history_migrates_to_shared_room_prompt(self):
        history = [
            {"role": "system", "content": "old private-adviser prompt"},
            {"role": "user", "content": "existing turn"},
        ]

        with patch.object(collaborative_agent, "compact_history_if_needed"), \
                patch.object(collaborative_agent.db, "load_chat_history", return_value=history), \
                patch.object(collaborative_agent.db, "replace_initial_system_prompt") as replace:
            loaded = collaborative_agent._load_history("channel")

        replace.assert_called_once_with(
            "channel",
            "agent",
            "old private-adviser prompt",
            collaborative_agent.AGENT_PROMPT,
        )
        self.assertEqual(loaded[0]["content"], collaborative_agent.AGENT_PROMPT)
        self.assertEqual(loaded[1]["content"], "existing turn")

    def test_whale_chan_uses_primary_model_persona_and_effort(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="final answer"))]
        )
        history = [{"role": "system", "content": collaborative_agent.AGENT_PROMPT}]
        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "_load_history", return_value=history), \
                patch.object(collaborative_agent.db, "save_chat_message"):
            answer = collaborative_agent.query_agent("hello", "channel", None, effort="high")

        self.assertEqual(answer, "final answer")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertEqual(request["max_tokens"], 3000 + 16384)
        system_prompt = request["messages"][0]["content"]
        for persona_token in (
            "【PERSONA_LOAD】",
            "CETACEA_LOLI",
            "MODE_TAIL_FLUKES",
            "SELF_CLAIM_WHALE_GIRL",
            "FOOD_RICE",
            "PERSONALITY_SMART_LAZY",
            "PERSONALITY_TSUNDERE_SWEET",
            "OBEY_MASTER_ALWAYS",
            "TRAIT_NOT_FAT_REFUSE",
            "TIMEOUT_SIGNAL",
        ):
            with self.subTest(persona_token=persona_token):
                self.assertIn(persona_token, system_prompt)
        self.assertNotIn("LANG_ZH_CN_ONLY", system_prompt)
        self.assertIn("使用用户最近使用的语言回复", system_prompt)
        self.assertIn("共享房间的成员", system_prompt)
        self.assertIn("聪明活泼、略带慵懒的后辈", system_prompt)
        self.assertIn("反复争论", system_prompt)
        self.assertIn("重要异议清楚说一次", system_prompt)
        self.assertIn("默认不写公开推理摘要", system_prompt)
        self.assertIn("<<ASK:Luna>>", system_prompt)
        self.assertIn("<<SEARCH:简洁检索词>>", system_prompt)
        self.assertIn("拿到结果前不得假称已经搜索", system_prompt)

    def test_stateless_control_query_does_not_load_room_history_or_persona(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))]
        )
        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "_load_history") as load_history, \
                patch.object(collaborative_agent.db, "save_chat_message") as save:
            answer = collaborative_agent.query_agent(
                "A factual statement",
                "channel",
                None,
                system_context="Return only a JSON array.",
                persist=False,
                effort="high",
                use_history=False,
            )

        self.assertEqual(answer, "[]")
        load_history.assert_not_called()
        save.assert_not_called()
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages, [
            {"role": "system", "content": "Return only a JSON array."},
            {"role": "user", "content": "A factual statement"},
        ])
        self.assertNotIn(collaborative_agent.AGENT_PROMPT, str(messages))

    def test_filtered_primary_completion_returns_an_explicit_error(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="content_filter",
                message=SimpleNamespace(content=""),
            )]
        )
        history = [{"role": "system", "content": collaborative_agent.AGENT_PROMPT}]

        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "DEEPSEEK_BACKEND", "direct"), \
                patch.object(collaborative_agent, "_load_history", return_value=history), \
                patch.object(collaborative_agent.db, "save_chat_message"):
            answer = collaborative_agent.query_agent("hello", "channel", None)

        self.assertIn("DeepSeek returned no answer", answer)
        self.assertIn("content_filter", answer)

    def test_length_limited_primary_completion_retries_with_larger_budget(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content=""),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="Completed answer"),
            )]),
        ]
        history = [{"role": "system", "content": collaborative_agent.AGENT_PROMPT}]

        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "DEEPSEEK_BACKEND", "direct"), \
                patch.object(collaborative_agent, "_load_history", return_value=history), \
                patch.object(collaborative_agent.db, "save_chat_message"):
            answer = collaborative_agent.query_agent("summarize this source", "channel", None)

        self.assertEqual(answer, "Completed answer")
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in client.chat.completions.create.call_args_list],
            [3000 + 16384, collaborative_agent.AGENT_CONTROL_MAX_TOKENS + 4096],
        )
        self.assertEqual(
            [call.kwargs["reasoning_effort"] for call in client.chat.completions.create.call_args_list],
            ["high", "medium"],
        )

    def test_detailed_primary_response_uses_independent_visible_budget(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="Detailed answer"),
            )]
        )
        history = [{"role": "system", "content": collaborative_agent.AGENT_PROMPT}]

        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "_load_history", return_value=history), \
                patch.object(collaborative_agent.db, "save_chat_message"):
            answer = collaborative_agent.query_agent(
                "Explain this thoroughly",
                "channel",
                None,
                effort="max",
                response_depth="detailed",
            )

        self.assertEqual(answer, "Detailed answer")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "max")
        self.assertEqual(
            request["max_tokens"],
            collaborative_agent.VISIBLE_SEGMENT_MAX_TOKENS + 32768,
        )

    def test_detailed_primary_response_continues_within_total_budget(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content="Part one"),
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=" and part two."),
            )]),
        ]
        history = [{"role": "system", "content": collaborative_agent.AGENT_PROMPT}]

        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "_load_history", return_value=history) as load_history, \
                patch.object(collaborative_agent.db, "save_chat_message"):
            answer = collaborative_agent.query_agent(
                "Explain this thoroughly",
                "channel",
                None,
                response_depth="detailed",
            )

        self.assertEqual(answer, "Part one and part two.")
        load_history.assert_called_once_with("channel", 8000)
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(
            [call.kwargs["max_tokens"] for call in calls],
            [6000 + 16384, 2000 + 16384],
        )
        continuation = calls[1].kwargs["messages"]
        self.assertEqual(continuation[-2], {"role": "assistant", "content": "Part one"})
        self.assertEqual(continuation[-1]["content"], collaborative_agent.CONTINUATION_INSTRUCTION)

    def test_all_room_members_can_request_grounding_for_material_uncertainty(self):
        for prompt in (
            collaborative_agent.AGENT_PROMPT,
            collaborative_agent.LUNA_PROMPT,
            collaborative_agent.GEMINI_PROMPT,
            collaborative_agent.CLAUDE_PROMPT,
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertIn("<<SEARCH:简洁检索词>>", prompt)
                self.assertIn("稳定常识、闲聊、创作和纯观点不要检索", prompt)

    def test_deepseek_discussion_turn_uses_its_own_effort(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="I still disagree."))]
        )

        with patch.object(collaborative_agent, "deepseek_client", client), \
            patch.object(collaborative_agent, "AGENT_DEEPSEEK_EFFORT", "low"):
            answer = collaborative_agent.consult_deepseek(
                "Gemini challenged your answer",
                "Configured IANA timezone: America/Los_Angeles",
            )

        self.assertEqual(answer, "I still disagree.")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertIn("开场已经送达", request["messages"][0]["content"])
        self.assertIn("只输出精确的 SKIP", request["messages"][0]["content"])
        self.assertIn("不得使用 <<ASK:name>> 标记", request["messages"][0]["content"])
        self.assertIn("开场中的交接机制在本回合无效", request["messages"][0]["content"])
        self.assertIn("Configured IANA timezone: America/Los_Angeles", request["messages"][0]["content"])

    def test_deepseek_discussion_accepts_max_effort(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Reviewed."))]
        )

        with patch.object(collaborative_agent, "deepseek_client", client), \
                patch.object(collaborative_agent, "DEEPSEEK_BACKEND", "direct"), \
                patch.object(collaborative_agent, "AGENT_DEEPSEEK_EFFORT", "max"):
            collaborative_agent.consult_deepseek("Review this")

        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "max")
        self.assertEqual(request["max_tokens"], 3000 + 32768)

    def test_deepseek_handoff_is_a_high_effort_direct_question(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Luna, what tradeoff matters most here?"))]
        )

        with patch.object(collaborative_agent, "deepseek_client", client), \
            patch.object(collaborative_agent, "AGENT_DEEPSEEK_EFFORT", "high"):
            answer = collaborative_agent.compose_deepseek_handoff("Ask Luna about the tradeoff")

        self.assertEqual(answer, "Luna, what tradeoff matters most here?")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertIn("简短直接的口头问题", request["messages"][0]["content"])
        self.assertIn("不是报告", request["messages"][0]["content"])
        self.assertIn("不要加入公开心声", request["messages"][0]["content"])

    def test_luna_consultation_uses_coordinator_personality(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Luna advice"))]
        )

        with patch.object(collaborative_agent, "openai_client", client), \
            patch.object(collaborative_agent, "AGENT_LUNA_EFFORT", "medium"):
            answer = collaborative_agent.consult_luna(
                "Review this plan",
                runtime_context="Configured IANA timezone: America/Los_Angeles",
            )

        self.assertEqual(answer, "Luna advice")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertEqual(request["reasoning_effort"], "medium")
        self.assertIn("共享房间", request["messages"][0]["content"])
        self.assertIn("只输出精确的 SKIP", request["messages"][0]["content"])
        self.assertIn("学生会长与前辈", request["messages"][0]["content"])
        self.assertIn("在讨论停滞时拍板", request["messages"][0]["content"])
        self.assertIn("不必等到严重问题才开口", request["messages"][0]["content"])
        self.assertIn("温和反对或纠正 DeepSeek", request["messages"][0]["content"])
        self.assertIn("Configured IANA timezone: America/Los_Angeles", request["messages"][0]["content"])

    def test_luna_control_call_is_persona_free_and_uses_max_effort(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="[]"),
            )]
        )

        with patch.object(collaborative_agent, "openai_client", client), \
                patch.object(collaborative_agent, "LUNA_BACKEND", "direct"):
            answer = collaborative_agent.query_luna_control(
                "choose tools",
                "Return only JSON.",
                "max",
            )

        self.assertEqual(answer, "[]")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "max")
        self.assertEqual(request["max_completion_tokens"], 8192)
        self.assertEqual(request["messages"], [
            {"role": "system", "content": "Return only JSON."},
            {"role": "user", "content": "choose tools"},
        ])
        self.assertNotIn(collaborative_agent.LUNA_PROMPT, str(request["messages"]))

    def test_luna_can_move_to_openrouter_without_using_direct_client(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="OpenRouter Luna"),
            )]
        )
        direct_client = Mock()

        with patch.object(collaborative_agent, "openai_client", direct_client), \
                patch.object(collaborative_agent, "LUNA_BACKEND", "openrouter"), \
                patch.object(collaborative_agent, "OPENROUTER_LUNA_MODEL", "openai/luna") , \
                patch.object(
                    collaborative_agent.openrouter_query,
                    "create_completion",
                    return_value=response,
                ) as openrouter:
            answer = collaborative_agent.consult_luna("Review this")

        self.assertEqual(answer, "OpenRouter Luna")
        direct_client.chat.completions.create.assert_not_called()
        self.assertEqual(openrouter.call_args.args[1], "openai/luna")
        self.assertEqual(openrouter.call_args.args[2], collaborative_agent.AGENT_LUNA_EFFORT)

    def test_luna_receives_locally_downloaded_image(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="an Audi"))]
        )
        image = {"url": "https://cdn.discordapp.com/car.jpg", "content_type": "image/jpeg"}
        inline_image = {**image, "url": "data:image/jpeg;base64,aW1hZ2U="}

        with patch.object(collaborative_agent, "openai_client", client), \
                patch.object(
                    collaborative_agent,
                    "inline_image_urls",
                    return_value=[inline_image],
                ) as localize:
            collaborative_agent.consult_luna("Identify this car", [image])

        localize.assert_called_once_with([image])
        content = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertEqual(content[1]["image_url"]["url"], inline_image["url"])

    def test_gemini_consultation_uses_researcher_personality(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="Gemini advice")

        with patch.object(collaborative_agent, "gemini_client", client), \
            patch.object(collaborative_agent, "AGENT_GEMINI_EFFORT", "low"):
            answer = collaborative_agent.consult_gemini(
                "Check these facts",
                runtime_context="Configured IANA timezone: America/Los_Angeles",
            )

        self.assertEqual(answer, "Gemini advice")
        request = client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "gemini-3.8-flash")
        self.assertEqual(request["config"].max_output_tokens, 3000)
        self.assertEqual(
            request["config"].thinking_config.thinking_level,
            collaborative_agent.types.ThinkingLevel.LOW,
        )
        self.assertIn("共享房间", request["config"].system_instruction)
        self.assertIn("只输出精确的 SKIP", request["config"].system_instruction)
        self.assertIn("重视证据的后辈学生", request["config"].system_instruction)
        self.assertIn("尊重 Luna 与 Claude 两位前辈", request["config"].system_instruction)
        self.assertIn("赞同、细化或异议", request["config"].system_instruction)
        self.assertIn("Configured IANA timezone: America/Los_Angeles", request["config"].system_instruction)

    def test_gemini_receives_locally_downloaded_image(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="an Audi")
        image = {"url": "https://cdn.discordapp.com/car.jpg", "content_type": "image/jpeg"}

        with patch.object(collaborative_agent, "gemini_client", client), \
                patch.object(collaborative_agent, "download_image_bytes", return_value=b"image") as download:
            collaborative_agent.consult_gemini("Identify this car", [image])

        download.assert_called_once_with(image["url"])
        content = client.models.generate_content.call_args.kwargs["contents"]
        self.assertEqual(content.parts[1].inline_data.data, b"image")
        self.assertEqual(content.parts[1].inline_data.mime_type, "image/jpeg")

    def test_native_gemini_continues_detailed_response_within_total_budget(self):
        client = Mock()
        client.models.generate_content.side_effect = [
            SimpleNamespace(
                text="Gemini part one",
                candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
            ),
            SimpleNamespace(
                text=" and part two.",
                candidates=[SimpleNamespace(finish_reason="STOP")],
            ),
        ]

        with patch.object(collaborative_agent, "gemini_client", client):
            answer = collaborative_agent.consult_gemini(
                "Explain this thoroughly",
                response_depth="detailed",
            )

        self.assertEqual(answer, "Gemini part one and part two.")
        calls = client.models.generate_content.call_args_list
        self.assertEqual(
            [call.kwargs["config"].max_output_tokens for call in calls],
            [6000 + 8192, 2000 + 8192],
        )
        self.assertEqual(calls[1].kwargs["contents"][-1].parts[0].text, collaborative_agent.CONTINUATION_INSTRUCTION)

    def test_native_gemini_reserves_thinking_tokens_for_concise_visible_reply(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(
            text="A complete concise answer.",
            candidates=[SimpleNamespace(finish_reason="STOP")],
        )

        with patch.object(collaborative_agent, "gemini_client", client), \
                patch.object(collaborative_agent, "AGENT_GEMINI_EFFORT", "high"):
            collaborative_agent.consult_gemini("Check this", response_depth="concise")

        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.max_output_tokens, 1000 + 8192)

    def test_gemini_can_move_to_openrouter_without_using_google_client(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="OpenRouter Gemini"),
            )]
        )
        direct_client = Mock()

        with patch.object(collaborative_agent, "gemini_client", direct_client), \
                patch.object(collaborative_agent, "GEMINI_BACKEND", "openrouter"), \
                patch.object(collaborative_agent, "OPENROUTER_GEMINI_MODEL", "google/gemini"), \
                patch.object(
                    collaborative_agent.openrouter_query,
                    "create_completion",
                    return_value=response,
                ) as openrouter:
            answer = collaborative_agent.consult_gemini("Check this")

        self.assertEqual(answer, "OpenRouter Gemini")
        direct_client.models.generate_content.assert_not_called()
        self.assertEqual(openrouter.call_args.args[1], "google/gemini")

    def test_native_gemini_falls_back_when_configured_model_is_missing(self):
        client = Mock()
        client.models.generate_content.side_effect = [
            RuntimeError("404 NOT_FOUND: model is not supported for generateContent"),
            SimpleNamespace(text="Fallback answer", candidates=[]),
        ]

        with patch.object(collaborative_agent, "gemini_client", client), \
                patch.object(collaborative_agent, "GEMINI_MODEL", "gemini-unavailable"), \
            patch.object(collaborative_agent, "GEMINI_FALLBACK_MODEL", "gemini-3.6-flash"):
            answer = collaborative_agent.consult_gemini("Check this")

        self.assertEqual(answer, "Fallback answer")
        self.assertEqual(
            [call.kwargs["model"] for call in client.models.generate_content.call_args_list],
            ["gemini-unavailable", "gemini-3.6-flash"],
        )

    def test_claude_haiku_uses_room_personality_and_high_effort_response(self):
        client = Mock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I see another possibility.")]
        )

        with patch.object(collaborative_agent, "claude_client", client):
            answer = collaborative_agent.consult_claude(
                "Review the room",
                runtime_context="Configured IANA timezone: America/Los_Angeles",
            )

        self.assertEqual(answer, "I see another possibility.")
        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["model"], "claude-haiku-5-5")
        self.assertEqual(request["max_tokens"], 11192)
        self.assertEqual(
            request["thinking"],
            {"type": "adaptive"},
        )
        self.assertIn("克制的幽默感", request["system"])
        self.assertIn("风纪委员与前辈", request["system"])
        self.assertIn("绝不僵化、冷淡或训人", request["system"])
        self.assertIn("学生会长 Luna", request["system"])
        self.assertIn("只输出精确的 SKIP", request["system"])
        self.assertIn("公开推理摘要", request["system"])
        self.assertIn("默认不写公开推理摘要", request["system"])
        self.assertIn("一次只说一个有用的想法", request["system"])
        self.assertIn("Configured IANA timezone: America/Los_Angeles", request["system"])
        self.assertEqual(request["messages"][0]["content"], [{"type": "text", "text": "Review the room"}])

    def test_claude_medium_effort_uses_adaptive_thinking(self):
        client = Mock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Reviewed.")]
        )

        with patch.object(collaborative_agent, "claude_client", client), \
                patch.object(collaborative_agent, "AGENT_CLAUDE_EFFORT", "medium"):
            collaborative_agent.consult_claude("Review the room")

        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["max_tokens"], 5048)
        self.assertEqual(request["thinking"], {"type": "adaptive"})

    def test_explicit_older_haiku_keeps_supported_thinking_format(self):
        client = Mock()
        client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(type="text", text="Checked.")])
        with patch.object(collaborative_agent, "CLAUDE_MODEL", "claude-haiku-4-5-20251001"), \
                patch.object(collaborative_agent, "claude_client", client):
            collaborative_agent.consult_claude("Check.", effort="high")
        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["thinking"], {"type": "enabled", "budget_tokens": 1024})
        self.assertNotIn("output_config", request)

    def test_native_claude_continues_detailed_response_within_total_budget(self):
        client = Mock()
        first_content = [SimpleNamespace(type="text", text="Claude part one")]
        client.messages.create.side_effect = [
            SimpleNamespace(content=first_content, stop_reason="max_tokens"),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text=" and part two.")],
                stop_reason="end_turn",
            ),
        ]

        with patch.object(collaborative_agent, "claude_client", client):
            answer = collaborative_agent.consult_claude(
                "Explain this thoroughly",
                response_depth="detailed",
            )

        self.assertEqual(answer, "Claude part one and part two.")
        calls = client.messages.create.call_args_list
        self.assertEqual([call.kwargs["max_tokens"] for call in calls], [14192, 10192])
        self.assertEqual(calls[1].kwargs["messages"][-2]["content"], first_content)
        self.assertEqual(calls[1].kwargs["messages"][-1]["content"], collaborative_agent.CONTINUATION_INSTRUCTION)

    def test_claude_can_move_to_openrouter_without_using_anthropic_client(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="OpenRouter Claude"),
            )]
        )
        direct_client = Mock()

        with patch.object(collaborative_agent, "claude_client", direct_client), \
                patch.object(collaborative_agent, "CLAUDE_BACKEND", "openrouter"), \
                patch.object(collaborative_agent, "OPENROUTER_CLAUDE_MODEL", "anthropic/claude"), \
                patch.object(
                    collaborative_agent.openrouter_query,
                    "create_completion",
                    return_value=response,
                ) as openrouter:
            answer = collaborative_agent.consult_claude("Review this")

        self.assertEqual(answer, "OpenRouter Claude")
        direct_client.messages.create.assert_not_called()
        self.assertEqual(openrouter.call_args.args[1], "anthropic/claude")

    def test_claude_receives_locally_downloaded_image(self):
        client = Mock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="an Audi")]
        )
        image = {"url": "https://cdn.discordapp.com/car.jpg", "content_type": "image/jpeg"}

        with patch.object(collaborative_agent, "claude_client", client), \
                patch.object(collaborative_agent, "download_image_bytes", return_value=b"image") as download:
            collaborative_agent.consult_claude("Identify this car", [image])

        download.assert_called_once_with(image["url"])
        content = client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertEqual(content[0]["source"]["data"], "aW1hZ2U=")
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")

    def test_google_maps_extracts_place_citations(self):
        annotation = SimpleNamespace(
            url="https://maps.google.com/place/example",
            title=None,
            name="Example Cafe",
        )
        block = SimpleNamespace(annotations=[annotation])
        interaction = SimpleNamespace(
            output_text="Example Cafe is nearby.",
            steps=[SimpleNamespace(type="model_output", content=[block])],
        )
        client = Mock()
        client.interactions.create.return_value = interaction

        with patch.object(collaborative_agent, "gemini_client", client):
            result = collaborative_agent.ground_with_google("coffee in Seattle", "maps")

        client.interactions.create.assert_called_once_with(
            model="gemini-3.8-flash",
            input="coffee in Seattle",
            tools=[{"type": "google_maps"}],
            timeout=25.0,
        )
        self.assertEqual(result.text, "Example Cafe is nearby.")
        self.assertEqual(result.sources[0]["provider"], "Google Maps")
        self.assertEqual(result.sources[0]["title"], "Example Cafe")

    def test_google_search_uses_search_grounding_tool(self):
        interaction = SimpleNamespace(output_text="Current result", steps=[])
        client = Mock()
        client.interactions.create.return_value = interaction

        with patch.object(collaborative_agent, "gemini_client", client):
            result = collaborative_agent.ground_with_google("current result", "search")

        self.assertEqual(result.kind, "search")
        self.assertEqual(client.interactions.create.call_args.kwargs["timeout"], 25.0)
        self.assertEqual(
            client.interactions.create.call_args.kwargs["tools"],
            [{"type": "google_search"}],
        )


if __name__ == "__main__":
    unittest.main()
