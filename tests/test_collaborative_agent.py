from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from providers import collaborative_agent


class CollaborativeAgentTests(unittest.TestCase):
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
        self.assertEqual(request["max_tokens"], 2000)
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
        self.assertIn("共享房间的首位发言者", system_prompt)
        self.assertIn("聪明活泼、略带慵懒的后辈", system_prompt)
        self.assertIn("反复争论", system_prompt)
        self.assertIn("重要异议清楚说一次", system_prompt)
        self.assertIn("每次面向房间的可见开场或讨论发言，都必须先写", system_prompt)
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
            answer = collaborative_agent.consult_deepseek("Gemini challenged your answer")

        self.assertEqual(answer, "I still disagree.")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertIn("高投入开场已经送达", request["messages"][0]["content"])
        self.assertIn("只输出精确的 SKIP", request["messages"][0]["content"])
        self.assertIn("不得使用 <<ASK:name>> 标记", request["messages"][0]["content"])
        self.assertIn("开场中的交接机制在本回合无效", request["messages"][0]["content"])

    def test_deepseek_handoff_is_a_high_effort_direct_question(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Luna, what tradeoff matters most here?"))]
        )

        with patch.object(collaborative_agent, "deepseek_client", client):
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
            answer = collaborative_agent.consult_luna("Review this plan")

        self.assertEqual(answer, "Luna advice")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertEqual(request["reasoning_effort"], "medium")
        self.assertIn("共享房间", request["messages"][0]["content"])
        self.assertIn("只输出精确的 SKIP", request["messages"][0]["content"])
        self.assertIn("学生会长与前辈", request["messages"][0]["content"])
        self.assertIn("在讨论停滞时拍板", request["messages"][0]["content"])
        self.assertIn("不必等到严重问题才开口", request["messages"][0]["content"])
        self.assertIn("温和反对或纠正 DeepSeek", request["messages"][0]["content"])

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
            answer = collaborative_agent.consult_gemini("Check these facts")

        self.assertEqual(answer, "Gemini advice")
        request = client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "gemini-3.5-flash-lite")
        self.assertEqual(
            request["config"].thinking_config.thinking_level,
            collaborative_agent.types.ThinkingLevel.LOW,
        )
        self.assertIn("共享房间", request["config"].system_instruction)
        self.assertIn("只输出精确的 SKIP", request["config"].system_instruction)
        self.assertIn("重视证据的后辈学生", request["config"].system_instruction)
        self.assertIn("尊重 Luna 与 Claude 两位前辈", request["config"].system_instruction)
        self.assertIn("赞同、细化或异议", request["config"].system_instruction)

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

    def test_claude_haiku_uses_room_personality_and_high_effort_response(self):
        client = Mock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I see another possibility.")]
        )

        with patch.object(collaborative_agent, "claude_client", client):
            answer = collaborative_agent.consult_claude("Review the room")

        self.assertEqual(answer, "I see another possibility.")
        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(request["max_tokens"], 3072)
        self.assertEqual(
            request["thinking"],
            {"type": "enabled", "budget_tokens": 1024},
        )
        self.assertIn("克制的幽默感", request["system"])
        self.assertIn("风纪委员与前辈", request["system"])
        self.assertIn("绝不僵化、冷淡或训人", request["system"])
        self.assertIn("学生会长 Luna", request["system"])
        self.assertIn("只输出精确的 SKIP", request["system"])
        self.assertIn("虚构公开心声", request["system"])
        self.assertEqual(request["messages"][0]["content"], [{"type": "text", "text": "Review the room"}])

    def test_claude_medium_effort_does_not_enable_extended_thinking(self):
        client = Mock()
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Reviewed.")]
        )

        with patch.object(collaborative_agent, "claude_client", client), \
                patch.object(collaborative_agent, "AGENT_CLAUDE_EFFORT", "medium"):
            collaborative_agent.consult_claude("Review the room")

        request = client.messages.create.call_args.kwargs
        self.assertEqual(request["max_tokens"], 2000)
        self.assertNotIn("thinking", request)

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
            model="gemini-3.5-flash-lite",
            input="coffee in Seattle",
            tools=[{"type": "google_maps"}],
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
        self.assertEqual(
            client.interactions.create.call_args.kwargs["tools"],
            [{"type": "google_search"}],
        )


if __name__ == "__main__":
    unittest.main()
