import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import discord
import yt_dlp

from services import database as db
from app.bot_types import BotResponse, HandlerResult, IncomingMessage
from parsers.command_parsers import parse_music_command


@dataclass
class MusicSession:
    text_channel_id: str
    song_index: int = 1
    song_current: str = ""
    discord_music: Any = None
    voice_client: Any = None
    play_list: List[str] = field(default_factory=list)
    runtime_playlist: List[str] = field(default_factory=list)


class MusicFeature:
    def __init__(self, music_list_path: str = "musicList.txt", music_folder: str = "music"):
        self.music_list_path = Path(music_list_path)
        self.music_folder = Path(music_folder)
        self.sessions: Dict[str, MusicSession] = {}

    def can_handle(self, message: IncomingMessage) -> bool:
        return message.content.startswith("$music")

    async def handle(self, app, message: IncomingMessage) -> HandlerResult:
        if not getattr(app.platform, "supports_voice", True):
            return [BotResponse(text="⚠️ Music and voice features are not supported on this platform.")]

        cmd = parse_music_command(message.content)
        if cmd.errors:
            return [BotResponse(text="❌ " + "\n".join(cmd.errors))]

        native_author = message.metadata.get("author_ref")
        native_client = message.metadata.get("native_client")
        session = self._get_or_create_session(message.channel_id)

        action = cmd.action
        if action == "playTest":
            return await self._handle_play_test(app, native_client)
        if action == "youtube":
            return await self._handle_youtube(message, native_author, session, cmd)
        if action == "init":
            return await self._handle_init(message, native_author, session)
        if action == "stop":
            return await self._handle_stop(message, session)
        if action == "name":
            return [BotResponse(text=f"Music player is playing: #{session.song_index} {session.song_current}")]
        if action == "play":
            return await self._handle_play(message, session)
        if action == "pause":
            return await self._handle_pause(message, session)
        if action == "next":
            return await self._handle_next(session)
        if action == "prev":
            return await self._handle_previous(session)

        return [BotResponse(text="❌ No music action specified")]

    def _get_or_create_session(self, text_channel_id: str) -> MusicSession:
        session = self.sessions.get(text_channel_id)
        if session is not None:
            return session

        play_list, song_index = self._load_playlist_file()
        state = db.load_music_state(text_channel_id)
        if state:
            song_index = state.get("current_song_index", song_index)

        session = MusicSession(
            text_channel_id=text_channel_id,
            song_index=song_index,
            play_list=play_list,
            runtime_playlist=play_list.copy(),
        )
        self.sessions[text_channel_id] = session
        if self._has_tracks(session):
            self._select_music(session, self._clamp(session, session.song_index))
        return session

    def _load_playlist_file(self):
        try:
            raw_lines = self.music_list_path.read_text(encoding="utf8").splitlines()
            play_list = [line for line in raw_lines if line != ""]
            song_index = 1
            if play_list and play_list[0].isdigit():
                song_index = int(play_list[0])
            elif not play_list:
                play_list = ["1"]
            return play_list, song_index
        except FileNotFoundError:
            return ["1"], 1

    async def _handle_play_test(self, app, native_client) -> HandlerResult:
        voice_channel_id = app.config.get("ID_VOICECHANNEL")
        if not voice_channel_id:
            return [BotResponse(text="❌ ID_VOICECHANNEL is not configured")]

        voice_channel = native_client.get_channel(int(voice_channel_id))
        if voice_channel is None:
            return [BotResponse(text="❌ Configured voice channel was not found")]

        voice_client = await voice_channel.connect()
        try:
            audio_path = str(self.music_folder / "EVA_OP.mp3")
            voice_client.play(discord.FFmpegPCMAudio(source=audio_path), after=lambda error: print("done", error))
            while voice_client.is_playing():
                await asyncio.sleep(0.5)
        finally:
            await voice_client.disconnect()

        return [BotResponse(text="Music Started testing")]

    async def _handle_youtube(self, message: IncomingMessage, native_author, session: MusicSession, cmd) -> HandlerResult:
        if not cmd.youtube_urls:
            return [BotResponse(text="❌ No YouTube URL provided")]

        if session.voice_client is None or not session.voice_client.is_connected():
            author_voice = getattr(native_author, "voice", None)
            if author_voice is None:
                return [BotResponse(text="You need to be in a voice channel first!")]
            session.voice_client = await author_voice.channel.connect()

        responses = []
        try:
            ytdl_options = {
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "opus"}],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }

            added_titles = []
            insert_position = session.song_index + 1
            with yt_dlp.YoutubeDL(ytdl_options) as ydl:
                for index, url in enumerate(cmd.youtube_urls):
                    try:
                        info = ydl.extract_info(url, download=False)
                        title = info.get("title", "Unknown")
                        added_titles.append(title)
                        if insert_position <= len(session.runtime_playlist):
                            session.runtime_playlist.insert(insert_position + index, url)
                        else:
                            session.runtime_playlist.append(url)
                    except Exception as error:
                        responses.append(BotResponse(text=f"⚠️ Failed to add URL {index + 1}: {error}"))

            if not added_titles:
                responses.append(BotResponse(text="❌ All URLs failed to load"))
                return responses

            if cmd.queue_only:
                if len(added_titles) == 1:
                    responses.append(BotResponse(text=f"➕ Added to queue: **{added_titles[0]}**"))
                else:
                    listing = "\n".join([f"{i + 1}. {title}" for i, title in enumerate(added_titles)])
                    responses.append(BotResponse(text=f"➕ Added {len(added_titles)} videos to queue:\n{listing}"))
                return responses

            if session.voice_client.is_playing():
                session.voice_client.stop()

            session.song_index = insert_position
            self._select_music(session, session.song_index)

            if len(added_titles) == 1:
                responses.append(BotResponse(text=f"🎵 Now playing: **{added_titles[0]}**"))
            else:
                responses.append(
                    BotResponse(
                        text=(
                            f"🎵 Now playing: **{added_titles[0]}**\n"
                            f"➕ Added {len(added_titles) - 1} more to queue"
                        )
                    )
                )
            return responses
        except Exception as error:
            responses.append(BotResponse(text=f"❌ Error processing YouTube videos: {error}"))
            return responses

    async def _handle_init(self, message: IncomingMessage, native_author, session: MusicSession) -> HandlerResult:
        author_voice = getattr(native_author, "voice", None)
        if author_voice is None:
            return [BotResponse(text="You need to be in a voice channel first!")]

        if session.voice_client and session.voice_client.is_connected():
            await session.voice_client.disconnect()

        session.voice_client = await author_voice.channel.connect()
        db.save_music_state(message.channel_id, str(author_voice.channel.id), session.song_index, False)

        if self._has_tracks(session):
            self._select_music(session, self._clamp(session, session.song_index))
        else:
            return [BotResponse(text=f"Music player initialized in {author_voice.channel.name}, but playlist is empty")]

        return [BotResponse(text=f"Music player initialized in {author_voice.channel.name}")]

    async def _handle_stop(self, message: IncomingMessage, session: MusicSession) -> HandlerResult:
        if session.voice_client is None:
            return [BotResponse(text="Music player is not initialized")]

        self._persist_playlist_state(session)
        db.clear_music_state(message.channel_id)

        if session.voice_client.is_connected():
            await session.voice_client.disconnect()
        session.voice_client = None
        return [BotResponse(text="Music player stopped")]

    async def _handle_play(self, message: IncomingMessage, session: MusicSession) -> HandlerResult:
        if session.voice_client is None or not session.voice_client.is_connected():
            return [BotResponse(text="Music player is not initialized. Use $music initialize first")]

        db.save_music_state(message.channel_id, str(session.voice_client.channel.id), session.song_index, True)
        if session.voice_client.is_paused():
            session.voice_client.resume()
        elif not session.voice_client.is_playing() and session.discord_music is not None:
            session.voice_client.play(session.discord_music, after=lambda error: self._next_song(session))
        return [BotResponse(text="Music player started")]

    async def _handle_pause(self, message: IncomingMessage, session: MusicSession) -> HandlerResult:
        if session.voice_client is None or not session.voice_client.is_connected():
            return [BotResponse(text="Music player is not initialized")]

        db.save_music_state(message.channel_id, str(session.voice_client.channel.id), session.song_index, False)
        if session.voice_client.is_playing():
            session.voice_client.pause()
        return [BotResponse(text="Music player paused")]

    async def _handle_next(self, session: MusicSession) -> HandlerResult:
        if session.voice_client is None or not session.voice_client.is_connected():
            return [BotResponse(text="Music player is not initialized")]
        if not self._has_tracks(session):
            return [BotResponse(text="Playlist is empty")]

        if session.voice_client.is_playing() or session.voice_client.is_paused():
            session.voice_client.stop()
        self._next_song(session)
        return [BotResponse(text="Music player is now playing the next song")]

    async def _handle_previous(self, session: MusicSession) -> HandlerResult:
        if session.voice_client is None or not session.voice_client.is_connected():
            return [BotResponse(text="Music player is not initialized")]
        if not self._has_tracks(session):
            return [BotResponse(text="Playlist is empty")]

        if session.voice_client.is_playing() or session.voice_client.is_paused():
            session.voice_client.stop()
        self._previous_song(session)
        return [BotResponse(text="Music player is now playing the previous song")]

    def _select_music(self, session: MusicSession, index: int) -> None:
        if not self._has_tracks(session):
            session.song_current = ""
            session.discord_music = None
            return

        session.song_index = index
        session.song_current = session.runtime_playlist[session.song_index]

        if session.song_current.startswith("http://") or session.song_current.startswith("https://"):
            ytdl_options = {
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "opus"}],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }
            ffmpeg_options = {
                "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                "options": "-vn",
            }
            with yt_dlp.YoutubeDL(ytdl_options) as ydl:
                info = ydl.extract_info(session.song_current, download=False)
                url = info["url"]
                session.discord_music = discord.FFmpegPCMAudio(url, **ffmpeg_options)
        else:
            session.discord_music = discord.FFmpegPCMAudio(source=str(self.music_folder / session.song_current))

        if (
            session.voice_client
            and session.voice_client.is_connected()
            and not session.voice_client.is_playing()
            and not session.voice_client.is_paused()
        ):
            session.voice_client.play(session.discord_music, after=lambda error: self._next_song(session))

    def _next_song(self, session: MusicSession) -> None:
        session.song_index += 1
        self._select_music(session, self._clamp(session, session.song_index))

    def _previous_song(self, session: MusicSession) -> None:
        session.song_index -= 1
        self._select_music(session, self._clamp(session, session.song_index))

    def _clamp(self, session: MusicSession, number: int) -> int:
        length = len(session.runtime_playlist)
        if length <= 1:
            return 1
        if number < 1:
            number = number + length - 1
        if number >= length:
            number = number - length + 1
        return number

    def _has_tracks(self, session: MusicSession) -> bool:
        return len(session.runtime_playlist) > 1

    def _persist_playlist_state(self, session: MusicSession) -> None:
        lines = [str(session.song_index)]
        lines.extend(session.play_list[1:])
        self.music_list_path.write_text("\n".join(lines), encoding="utf8")
