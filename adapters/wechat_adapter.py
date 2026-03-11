import asyncio
import os
import re
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from app.bot_types import IncomingMessage


@dataclass
class WeChatAuthorReference:
    channel_id: str
    display_name: str


class WeChatAdapter:
    supports_message_edit = False
    supports_voice = False
    supports_markdown = False

    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.app.attach_platform(self)

        self.primary_chat = str(config.get("WECHAT_CHAT", "")).strip()
        self.idle_chat = str(config.get("WECHAT_IDLE_CHAT", "File Transfer")).strip() or "File Transfer"
        self.monitor_seconds = max(1, int(config.get("WECHAT_MONITOR_SECONDS", 3)))
        self.whitelist = self._parse_whitelist(config.get("WECHAT_WHITELIST", []))
        self._pywechat_loaded = False
        self._recent_outbound: List[tuple[str, str, float]] = []
        self._recent_inbound: List[tuple[str, str, float]] = []
        self._ui_locale = "unknown"
        self._ui_lock = asyncio.Lock()
        self._chat_queues: dict[str, asyncio.Queue[str]] = {}
        self._chat_workers: dict[str, asyncio.Task] = {}
        self._whitelist_pull_count = 3
        self._whitelist_watch_seconds = max(1, min(3, self.monitor_seconds))

    def _parse_whitelist(self, raw_value) -> set[str]:
        if isinstance(raw_value, list):
            return {str(item).strip() for item in raw_value if str(item).strip()}

        if not raw_value:
            return set()

        text = str(raw_value)
        parts = re.split(r"[,\n;|]", text)
        return {part.strip() for part in parts if part.strip()}

    def _is_chat_allowed(self, channel_id: str) -> bool:
        return not self.whitelist or channel_id in self.whitelist

    def _patch_pywechat_for_current_ui(self) -> None:
        from pywinauto import Desktop
        import pyweixin.WeChatAuto as wechat_auto
        import pyweixin.WeChatTools as wechat_tools
        import pyweixin.utils as wechat_utils

        desktop = Desktop(backend="uia")
        if desktop.window(title="WeChat", class_name="mmui::MainWindow").exists(timeout=1):
            self._ui_locale = "en"
        elif desktop.window(title="微信", class_name="mmui::MainWindow").exists(timeout=1):
            self._ui_locale = "zh"
        else:
            self._ui_locale = "unknown"

        if self._ui_locale != "en":
            return

        localized_fields = {
            "Chats": {"title": "WeChat", "control_type": "Button", "class_name": "mmui::XTabBarItem"},
            "Contacts": {"title": "Contacts", "control_type": "Button"},
            "Collections": {"title": "Favorites", "control_type": "Button", "class_name": "mmui::XTabBarItem"},
            "Moments": {"title": "Moments", "control_type": "Button", "class_name": "mmui::XTabBarItem"},
            "Search": {"title": "Search", "control_type": "Button", "class_name": "mmui::XTabBarItem"},
            "MiniProgram": {"title": "Mini Programs Panel", "control_type": "Button", "class_name": "mmui::XTabBarItem"},
            "More": {"title": "More", "control_type": "Button", "found_index": 0},
        }

        for module in (wechat_tools, wechat_auto, wechat_utils):
            for field_name, value in localized_fields.items():
                setattr(module.SideBar, field_name, value.copy())

            module.Main_window.MainWindow = {"title": "WeChat", "class_name": "mmui::MainWindow"}
            module.Main_window.Toolbar = {"title": "Navigation", "control_type": "ToolBar"}
            module.Main_window.SessionList = {"control_type": "List", "auto_id": "session_list"}
            module.Main_window.Search = {"control_type": "Edit", "class_name": "mmui::XValidatorTextEdit"}
            module.Main_window.FriendChatList = {"title": "Messages", "control_type": "List"}
            module.Lists.FriendChatList = {"title": "Messages", "control_type": "List"}

        def get_search_result(friend: str, search_result):
            candidates = []
            for listitem in search_result.children(control_type="ListItem"):
                if listitem.window_text() != friend:
                    continue
                if listitem.class_name() in {"mmui::SearchContentCellView", "mmui::XTableCell", "mmui::ChatSessionCell"}:
                    candidates.append(listitem)
            return candidates[0] if candidates else None

        wechat_tools.Tools.get_search_result = staticmethod(get_search_result)
        wechat_utils.Tools.get_search_result = staticmethod(get_search_result)

    def _load_pywechat(self) -> None:
        if self._pywechat_loaded:
            return

        if os.name != "nt":
            raise RuntimeError("The WeChat adapter only supports Windows.")

        repo_path = Path(__file__).resolve().parents[1] / "pywechat"
        if not repo_path.exists():
            raise RuntimeError("pywechat/ was not found in the workspace. Clone it locally first.")

        repo_str = str(repo_path)
        if repo_str not in sys.path:
            sys.path.insert(0, repo_str)

        try:
            from pyweixin import Files, GlobalConfig, Messages, Monitor, Navigator
        except Exception as error:
            raise RuntimeError(f"Failed to import pywechat/pyweixin dependencies: {error}") from error

        self._patch_pywechat_for_current_ui()

        GlobalConfig.close_weixin = False
        GlobalConfig.is_maximize = False

        self.Files = Files
        self.Messages = Messages
        self.Monitor = Monitor
        self.Navigator = Navigator
        self._pywechat_loaded = True

    def _normalize_text(self, text: str | None) -> str:
        if not text:
            return ""

        normalized = text.replace("```md", "").replace("```text", "").replace("```ml", "").replace("```", "")
        normalized = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1: \2", normalized)
        normalized = normalized.replace("**", "").replace("__", "").replace("`", "")
        return normalized.strip()

    def _remember_outbound(self, channel_id: str, text: str) -> None:
        normalized = self._normalize_text(text)
        if not normalized:
            return

        now = time.time()
        self._recent_outbound.append((channel_id, normalized, now))
        self._recent_outbound = [item for item in self._recent_outbound if now - item[2] <= 30]

    def _is_recent_outbound(self, channel_id: str, text: str) -> bool:
        normalized = self._normalize_text(text)
        now = time.time()
        self._recent_outbound = [item for item in self._recent_outbound if now - item[2] <= 30]
        return any(saved_channel == channel_id and saved_text == normalized for saved_channel, saved_text, _ in self._recent_outbound)

    def _remember_inbound(self, channel_id: str, text: str) -> None:
        normalized = self._normalize_text(text)
        if not normalized:
            return

        now = time.time()
        self._recent_inbound.append((channel_id, normalized, now))
        self._recent_inbound = [item for item in self._recent_inbound if now - item[2] <= 300]

    def _has_seen_inbound(self, channel_id: str, text: str) -> bool:
        normalized = self._normalize_text(text)
        if not normalized:
            return False

        now = time.time()
        self._recent_inbound = [item for item in self._recent_inbound if now - item[2] <= 300]
        return any(saved_channel == channel_id and saved_text == normalized for saved_channel, saved_text, _ in self._recent_inbound)

    async def _send_text_message_unlocked(self, channel_id: str, content: str) -> None:
        self._load_pywechat()
        normalized = self._normalize_text(content)
        if not normalized:
            return

        await asyncio.to_thread(
            self.Messages.send_messages_to_friend,
            friend=channel_id,
            messages=[normalized],
            close_weixin=False,
        )
        self._remember_outbound(channel_id, normalized)

    async def _send_file_message_unlocked(self, channel_id: str, response) -> None:
        self._load_pywechat()
        attachment = response.attachment
        normalized = self._normalize_text(response.text)

        try:
            await asyncio.to_thread(
                self.Files.send_files_to_friend,
                friend=channel_id,
                files=[attachment.path],
                with_messages=bool(normalized),
                messages=[normalized] if normalized else [""],
                close_weixin=False,
            )
            if normalized:
                self._remember_outbound(channel_id, normalized)
        finally:
            if attachment.delete_after_send and os.path.exists(attachment.path):
                os.remove(attachment.path)

    async def _send_responses_unlocked(self, channel_id: str, responses) -> None:
        for response in responses:
            if response.attachment is not None:
                await self._send_file_message_unlocked(channel_id, response)
                continue

            if response.text is not None:
                await self._send_text_message_unlocked(channel_id, response.text)

    async def _send_responses(self, channel_id: str, responses) -> None:
        async with self._ui_lock:
            await self._send_responses_unlocked(channel_id, responses)
            await self._park_on_idle_chat_unlocked(except_channel=channel_id)

    async def send_channel_message(self, channel_id: str, content: str) -> Any:
        async with self._ui_lock:
            await self._send_text_message_unlocked(channel_id, content)
            await self._park_on_idle_chat_unlocked(except_channel=channel_id)
        return channel_id

    async def edit_message(self, native_message, content: str) -> None:
        channel_id = native_message if isinstance(native_message, str) else self.primary_chat
        async with self._ui_lock:
            await self._send_text_message_unlocked(channel_id, f"[update] {content}")
            await self._park_on_idle_chat_unlocked(except_channel=channel_id)

    async def send_direct_message(self, user_reference, content: str) -> None:
        channel_id = getattr(user_reference, "channel_id", None) or self.primary_chat
        async with self._ui_lock:
            await self._send_text_message_unlocked(channel_id, f"[reminder] {content}")
            await self._park_on_idle_chat_unlocked(except_channel=channel_id)

    def create_background_task(self, coroutine):
        return asyncio.create_task(coroutine)

    async def _open_primary_dialog(self):
        self._load_pywechat()
        try:
            return await asyncio.to_thread(
                self.Navigator.open_dialog_window,
                friend=self.primary_chat,
                is_maximize=False,
                search_pages=20,
            )
        except Exception as error:
            details = [
                f"Failed to open WeChat chat '{self.primary_chat}'.",
                "Set WECHAT_CHAT to the exact WeChat chat or group name.",
                "Make sure the WeChat desktop app is logged in and that the chat is visible in the Chats list.",
            ]
            if self._ui_locale == "en":
                details.append("English WeChat UI was detected and compatibility patches were applied.")
            raise RuntimeError(" ".join(details)) from error

    async def _open_chat_dialog(self, channel_id: str):
        self._load_pywechat()
        try:
            return await asyncio.to_thread(
                self.Navigator.open_dialog_window,
                friend=channel_id,
                is_maximize=False,
                search_pages=20,
            )
        except Exception as error:
            raise RuntimeError(f"Failed to open WeChat chat '{channel_id}'.") from error

    async def _park_on_idle_chat_unlocked(self, except_channel: str | None = None) -> None:
        self._load_pywechat()

        if not self.idle_chat or self.primary_chat:
            return

        if except_channel and except_channel == self.idle_chat:
            return

        try:
            await asyncio.to_thread(
                self.Navigator.open_dialog_window,
                friend=self.idle_chat,
                is_maximize=False,
                search_pages=20,
            )
        except Exception as error:
            print(f"WeChat idle chat park skipped for {self.idle_chat}: {error}")

    async def _poll_messages(self, dialog_window, duration_seconds: int | None = None) -> List[str]:
        watch_seconds = max(1, int(duration_seconds or self.monitor_seconds))
        details = await asyncio.to_thread(
            self.Monitor.listen_on_chat,
            dialog_window,
            f"{watch_seconds}s",
            False,
            False,
            None,
            False,
        )
        return details.get("文本内容", [])

    async def _pull_recent_messages(self, channel_id: str, count: int | None = None) -> List[str]:
        self._load_pywechat()
        pull_count = max(1, int(count or self._whitelist_pull_count))
        messages = await asyncio.to_thread(
            self.Messages.pull_messages,
            channel_id,
            pull_count,
            20,
            False,
            False,
        )
        text_messages = [message for message in messages if isinstance(message, str) and message.strip()]
        return list(reversed(text_messages))

    def _extract_unread_count(self, session_text: str) -> int:
        patterns = [
            r"\[(\d+)(?:条)?\]",
            r"(\d+)条新消息",
            r"(\d+) unread",
        ]
        for pattern in patterns:
            match = re.search(pattern, session_text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return 0

    def _scan_unread_chat_counts(self) -> dict[str, int]:
        self._load_pywechat()
        main_window = self.Navigator.open_weixin(is_maximize=False)
        chats_title = "WeChat" if self._ui_locale == "en" else "微信"
        chats_button = main_window.child_window(title=chats_title, control_type="Button", class_name="mmui::XTabBarItem")
        if chats_button.exists(timeout=0.2):
            chats_button.click_input()

        session_list = main_window.child_window(control_type="List", auto_id="session_list")
        if not session_list.exists(timeout=1):
            candidates = main_window.descendants(control_type="List", auto_id="session_list")
            if not candidates:
                raise RuntimeError("Unable to find the WeChat session list while scanning unread chats.")
            session_list = candidates[0]

        session_list.type_keys("{HOME}")
        time.sleep(0.3)

        unread_counts: dict[str, int] = {}
        previous_last_item = None

        for _ in range(50):
            items = session_list.children(control_type="ListItem")
            if not items:
                break

            for item in items:
                automation_id = item.automation_id() or ""
                if not automation_id.startswith("session_item_"):
                    continue

                session_text = item.window_text() or ""
                if "消息免打扰" in session_text or "Mute Notifications" in session_text:
                    continue

                unread_count = self._extract_unread_count(session_text)
                if unread_count <= 0:
                    continue

                channel_id = automation_id.replace("session_item_", "", 1)
                unread_counts[channel_id] = max(unread_counts.get(channel_id, 0), unread_count)

            last_item = items[-1].automation_id() or items[-1].window_text()
            if last_item == previous_last_item:
                break

            previous_last_item = last_item
            session_list.type_keys("{PGDN}")
            time.sleep(0.3)

        session_list.type_keys("{HOME}")
        return unread_counts

    async def _poll_all_unread_messages(self) -> dict[str, List[str]]:
        self._load_pywechat()
        normalized: dict[str, List[str]] = {}
        async with self._ui_lock:
            unread_counts = await asyncio.to_thread(self._scan_unread_chat_counts)

            for channel_id, unread_count in unread_counts.items():
                if not self._is_chat_allowed(channel_id):
                    continue

                try:
                    messages = await asyncio.to_thread(
                        self.Messages.pull_messages,
                        channel_id,
                        unread_count,
                        20,
                        False,
                        False,
                    )
                except Exception as error:
                    print(f"WeChat unread scan skipped {channel_id}: {error}")
                    continue

                text_messages = [message for message in messages if isinstance(message, str) and message.strip()]
                if text_messages:
                    normalized[channel_id] = text_messages

            await self._park_on_idle_chat_unlocked()
        return normalized

    async def _poll_whitelist_messages(self) -> dict[str, List[str]]:
        self._load_pywechat()
        normalized: dict[str, List[str]] = {}

        async with self._ui_lock:
            for channel_id in sorted(self.whitelist):
                try:
                    dialog_window = await self._open_chat_dialog(channel_id)
                    recent_messages = await self._pull_recent_messages(channel_id, self._whitelist_pull_count)
                    live_messages = await self._poll_messages(dialog_window, self._whitelist_watch_seconds)
                except Exception as error:
                    print(f"WeChat whitelist poll skipped {channel_id}: {error}")
                    continue

                combined_messages: List[str] = []
                for text in recent_messages + live_messages:
                    if not isinstance(text, str) or not text.strip():
                        continue
                    if any(self._normalize_text(saved) == self._normalize_text(text) for saved in combined_messages):
                        continue
                    combined_messages.append(text)

                text_messages = [text for text in combined_messages if not self._has_seen_inbound(channel_id, text)]
                if text_messages:
                    normalized[channel_id] = text_messages

            await self._park_on_idle_chat_unlocked()

        return normalized

    async def _process_incoming_text(self, channel_id: str, text: str) -> None:
        if not text or self._is_recent_outbound(channel_id, text):
            return

        if not self._is_chat_allowed(channel_id):
            return

        author_ref = WeChatAuthorReference(channel_id=channel_id, display_name=channel_id)
        incoming = IncomingMessage(
            content=text,
            channel_id=channel_id,
            author_id=channel_id,
            author_name=channel_id,
            author_display_name=channel_id,
            created_at=datetime.now(timezone.utc),
            author_is_bot=False,
            guild_name="WeChat",
            channel_name=channel_id,
            is_dm=False,
            metadata={
                "native_message": channel_id,
                "native_channel": channel_id,
                "native_client": None,
                "author_ref": author_ref,
                "guild_ref": None,
                "guild_premium_tier": 0,
            },
        )

        responses = await self.app.handle_message(incoming)
        await self._send_responses(channel_id, responses)

    def _ensure_chat_worker(self, channel_id: str) -> asyncio.Queue[str]:
        queue = self._chat_queues.get(channel_id)
        if queue is None:
            queue = asyncio.Queue()
            self._chat_queues[channel_id] = queue

        worker = self._chat_workers.get(channel_id)
        if worker is None or worker.done():
            self._chat_workers[channel_id] = asyncio.create_task(self._chat_worker(channel_id, queue))

        return queue

    async def _enqueue_incoming_text(self, channel_id: str, text: str) -> None:
        if not text or not self._is_chat_allowed(channel_id):
            return

        if self._has_seen_inbound(channel_id, text):
            return

        self._remember_inbound(channel_id, text)

        queue = self._ensure_chat_worker(channel_id)
        await queue.put(text)

    async def _chat_worker(self, channel_id: str, queue: asyncio.Queue[str]) -> None:
        while True:
            text = await queue.get()
            try:
                await self._process_incoming_text(channel_id, text)
            except Exception as error:
                print(f"WeChat worker error for {channel_id}: {error}")
            finally:
                queue.task_done()

    async def _run_single_chat_loop(self) -> None:
        dialog_window = await self._open_primary_dialog()
        print(f"WeChat adapter is monitoring: {self.primary_chat}")
        if self.whitelist:
            print(f"WeChat whitelist enabled: {sorted(self.whitelist)}")

        while True:
            texts = await self._poll_messages(dialog_window)
            for text in texts:
                await self._enqueue_incoming_text(self.primary_chat, text)

            await asyncio.sleep(0.2)

    async def _run_global_scan_loop(self) -> None:
        print("WeChat adapter is scanning all unread chats")
        if self.whitelist:
            print(f"WeChat whitelist enabled: {sorted(self.whitelist)}")
        print(f"WeChat idle chat: {self.idle_chat}")

        async with self._ui_lock:
            await self._park_on_idle_chat_unlocked()

        while True:
            unread = await self._poll_all_unread_messages()
            for channel_id, messages in unread.items():
                for text in messages:
                    await self._enqueue_incoming_text(channel_id, text)

            await asyncio.sleep(max(1, self.monitor_seconds))

    async def _run_whitelist_loop(self) -> None:
        print("WeChat adapter is polling whitelist chats")
        print(f"WeChat whitelist enabled: {sorted(self.whitelist)}")
        print(f"WeChat idle chat: {self.idle_chat}")

        async with self._ui_lock:
            await self._park_on_idle_chat_unlocked()

        while True:
            messages_by_chat = await self._poll_whitelist_messages()
            for channel_id, messages in messages_by_chat.items():
                for text in messages:
                    await self._enqueue_incoming_text(channel_id, text)

            await asyncio.sleep(max(1, self.monitor_seconds))

    async def _shutdown_workers(self) -> None:
        for worker in self._chat_workers.values():
            worker.cancel()
        for worker in self._chat_workers.values():
            with suppress(asyncio.CancelledError):
                await worker

    async def _run_loop(self) -> None:
        try:
            if self.primary_chat:
                await self._run_single_chat_loop()
                return

            if self.whitelist:
                await self._run_whitelist_loop()
                return

            await self._run_global_scan_loop()
        finally:
            await self._shutdown_workers()

    def run(self):
        asyncio.run(self._run_loop())