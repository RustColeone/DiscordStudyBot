"""Music in the requester's voice channel (ported from the legacy `$music`).

One player per server: a looping track list (local files from `music_dir`, seeded from the legacy
`music_list` file, plus YouTube URLs added at runtime) kept in the interface bottle, played with
yt-dlp and ffmpeg. Voice needs PyNaCl, davey, libopus and ffmpeg; without them commands that need
voice fail with a clear message and nothing else is affected.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord

from contracts import ToolResult

AUDIO_SUFFIXES = {".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".webm", ".aac"}
YTDL_OPTIONS = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True}
STREAM_OPTIONS = {"before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", "options": "-vn"}
STREAM_TTL_S = 600
MAX_TRACKS = 200
FLAGS = {"--init": "init", "--initialize": "init", "-i": "init", "--play": "play", "-p": "play",
         "--pause": "pause", "--stop": "stop", "-s": "stop", "--next": "next", "-n": "next", "--skip": "next",
         "--prev": "prev", "--previous": "prev", "--name": "status", "--status": "status", "--list": "queue"}
WORDS = {"init": "init", "initialize": "init", "join": "init", "play": "play", "resume": "play", "pause": "pause",
         "stop": "stop", "leave": "stop", "next": "next", "skip": "next", "prev": "prev", "previous": "prev",
         "back": "prev", "name": "status", "status": "status", "now": "status", "queue": "queue", "list": "queue",
         "help": "help"}
CONTROL_ACTIONS = {"initialize": "init", "play": "play", "pause": "pause", "stop": "stop", "next": "next",
                   "previous": "prev"}
USAGE = ("**$music** — the music player in your voice channel\n"
         "`$music init` join your voice channel and play · `$music play` / `pause` / `stop`\n"
         "`$music next` / `prev` · `$music status` · `$music queue` (list)\n"
         "`$music -y <url> [url…]` play YouTube now · `$music -y <url> --queue` add after the current song")


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


@dataclass
class MusicCommand:
    action: str | None = None
    urls: list[str] = field(default_factory=list)
    queue_only: bool = False
    errors: list[str] = field(default_factory=list)


def parse_music_command(text: str) -> MusicCommand:
    """Legacy flags (`--init`, `-p`, `--pause`, `-s`, `-n`, `--prev`, `--name`, `-y <urls>`, `--queue`)
    and words (`init`, `play`, `pause`, `stop`, `next`, `prev`, `name`/`status`, `queue`, or bare URLs)."""
    cmd = MusicCommand()
    text = text.strip()
    if text.lower().startswith("$music"):
        text = text[6:].strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for index, token in enumerate(tokens):
        lowered = token.lower()
        if lowered in ("--youtube", "-y"):
            cmd.action = "youtube"
        elif lowered in ("--queue", "--add-next", "-q"):
            cmd.queue_only = True
        elif lowered in FLAGS:
            cmd.action = FLAGS[lowered]
        elif is_url(token):
            cmd.urls.append(token)
        elif index == 0 and lowered in WORDS:
            cmd.action = WORDS[lowered]
        else:
            cmd.errors.append(f"Unknown option: {token}")
    if cmd.urls:
        if cmd.action == "queue":
            cmd.action, cmd.queue_only = "youtube", True
        elif cmd.action in (None, "play", "youtube"):
            cmd.action = "youtube"
        else:
            cmd.errors.append("URLs only go with -y, play or queue")
    elif cmd.action == "youtube":
        cmd.errors.append("--youtube requires at least one URL")
    elif cmd.action is None:
        cmd.action = "queue" if cmd.queue_only else "help"
    return cmd


@dataclass
class Session:
    guild_id: str
    tracks: list[dict]
    index: int = 0
    voice: Any = None
    generation: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def connected(self) -> bool:
        return self.voice is not None and self.voice.is_connected()

    def current(self) -> dict | None:
        return self.tracks[self.index] if self.tracks else None


class MusicPlayer:
    """Settings (extension_config.discord): `music_dir` (`<data>/music`), `music_list` (none; the legacy
    `musicList.txt` format: optional first line = 1-based current index, then file names or URLs),
    `ffmpeg` (`ffmpeg`)."""

    def __init__(self, interface, music_dir: Path, music_list: Path | None = None, ffmpeg: str = "ffmpeg"):
        self.interface = interface
        self.music_dir = Path(music_dir)
        self.music_list = Path(music_list) if music_list else None
        self.ffmpeg = ffmpeg
        self.sessions: dict[str, Session] = {}
        self._streams: dict[str, tuple[str, float]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- platform hooks (replaced in tests) ------------------------------------------------------
    def voice_problem(self) -> str | None:
        """Why voice playback cannot work on this host, or None."""
        try:
            from discord import voice_client
            if not getattr(voice_client, "has_nacl", False):
                return "PyNaCl is not installed (pip install PyNaCl)."
            from discord import voice_state
            if not getattr(voice_state, "has_dave", True):
                return "The davey library is not installed (pip install davey)."
        except ImportError:
            pass
        if not discord.opus.is_loaded():
            try:
                loaded = discord.opus._load_default()
            except Exception:
                loaded = False
            if not loaded:
                return "libopus was not found (install libopus0)."
        if shutil.which(self.ffmpeg) is None:
            return f"ffmpeg was not found ({self.ffmpeg})."
        return None

    def extract(self, url: str) -> dict:
        """yt-dlp metadata for one URL (blocking)."""
        import yt_dlp
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as downloader:
            return downloader.extract_info(url, download=False)

    def make_source(self, location: str, stream: bool):
        options = STREAM_OPTIONS if stream else {}
        return discord.FFmpegPCMAudio(location, executable=self.ffmpeg, **options)

    # ---- state ------------------------------------------------------------------------------------
    @property
    def _bottle(self):
        port = self.interface.port
        return getattr(port, "bottle", None)

    def _track(self, entry: str) -> dict:
        return {"source": entry, "title": entry if is_url(entry) else Path(entry).stem}

    def _seed(self) -> tuple[list[dict], int]:
        if self.music_list is not None and self.music_list.is_file():
            lines = [line.strip() for line in self.music_list.read_text(encoding="utf8").splitlines() if line.strip()]
            index = int(lines.pop(0)) - 1 if lines and lines[0].isdigit() else 0
            return [self._track(line) for line in lines], index
        if self.music_dir.is_dir():
            files = sorted(path.name for path in self.music_dir.iterdir() if path.suffix.lower() in AUDIO_SUFFIXES)
            return [self._track(name) for name in files], 0
        return [], 0

    def session(self, guild_id: str) -> Session:
        session = self.sessions.get(guild_id)
        if session is None:
            stored = self._bottle.get(f"music:{guild_id}") if self._bottle is not None else None
            if stored:
                tracks, index = list(stored.get("tracks") or []), int(stored.get("index") or 0)
            else:
                tracks, index = self._seed()
            session = Session(guild_id, tracks[:MAX_TRACKS], index if 0 <= index < len(tracks) else 0)
            self.sessions[guild_id] = session
        return session

    def _save(self, session: Session) -> None:
        if self._bottle is not None:
            self._bottle.set(f"music:{session.guild_id}", {"tracks": session.tracks[:MAX_TRACKS],
                                                           "index": session.index})

    # ---- playback ---------------------------------------------------------------------------------
    async def _source(self, track: dict):
        location = track["source"]
        if not is_url(location):
            path = self.music_dir / location
            if not path.is_file():
                raise FileNotFoundError(f"{location} is not in the music folder")
            return self.make_source(str(path), stream=False)
        cached = self._streams.get(location)
        if cached is None or time.monotonic() - cached[1] > STREAM_TTL_S:
            info = await asyncio.to_thread(self.extract, location)
            track["title"] = info.get("title") or track["title"]
            cached = (info["url"], time.monotonic())
            self._streams[location] = cached
        return self.make_source(cached[0], stream=True)

    async def _play_current(self, session: Session, skip_failures: bool = False) -> str | None:
        """Play the current track (stopping whatever plays). Returns an error text, or None."""
        session.generation += 1
        generation = session.generation
        self._loop = asyncio.get_running_loop()
        error = "Playlist is empty"
        for _ in range(len(session.tracks)):
            track = session.current()
            try:
                source = await self._source(track)
            except Exception as failure:
                error = f"Could not play {track['title']}: {type(failure).__name__}: {failure}"[:300]
                if not skip_failures:
                    return error
                session.index = (session.index + 1) % len(session.tracks)
                continue
            if generation != session.generation or not session.connected():
                return None
            if session.voice.is_playing() or session.voice.is_paused():
                session.voice.stop()
            session.voice.play(source, after=lambda failure, mark=generation: self._finished(session, mark))
            self._save(session)
            return None
        return error

    def _finished(self, session: Session, generation: int) -> None:
        # Called from discord.py's player thread when a track ends.
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._spawn, session, generation)

    def _spawn(self, session: Session, generation: int) -> None:
        task = asyncio.get_running_loop().create_task(self._advance(session, generation))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _advance(self, session: Session, generation: int) -> None:
        async with session.lock:
            if generation != session.generation or not session.connected() or not session.tracks:
                return
            session.index = (session.index + 1) % len(session.tracks)
            error = await self._play_current(session, skip_failures=True)
            if error:
                self.interface.port.log("music_track_failed", guild=session.guild_id)

    async def _connect(self, session: Session, channel) -> None:
        if session.connected():
            if getattr(session.voice, "channel", None) == channel:
                return
            session.generation += 1
            await session.voice.disconnect()
        session.voice = await channel.connect()

    async def close(self) -> None:
        for session in self.sessions.values():
            session.generation += 1
            if session.connected():
                try:
                    session.voice.stop()
                    await session.voice.disconnect(force=True)
                except Exception:
                    pass
            session.voice = None
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- commands ---------------------------------------------------------------------------------
    def describe(self, session: Session) -> str:
        track = session.current()
        if track is None:
            return "Playlist is empty."
        voice = session.voice
        if not session.connected():
            state = "not connected"
        else:
            state = "paused" if voice.is_paused() else "playing" if voice.is_playing() else "stopped"
            state += f" in {getattr(voice.channel, 'name', 'voice')}"
        return f"Music player is {state}: #{session.index + 1} {track['title']}"

    def listing(self, session: Session, limit: int = 15) -> str:
        if not session.tracks:
            return "Playlist is empty."
        start = max(0, min(session.index - 2, len(session.tracks) - limit))
        lines = [f"{'▶' if number == session.index else ' '} {number + 1}. {track['title']}"
                 for number, track in enumerate(session.tracks[start:start + limit], start)]
        more = len(session.tracks) - start - limit
        return "\n".join([f"**Playlist** ({len(session.tracks)} songs)", *lines]
                         + ([f"… {more} more"] if more > 0 else []))

    async def run(self, command: MusicCommand, guild, voice_channel) -> ToolResult:
        """Run one parsed command in a server. `voice_channel` is the requester's, or None."""
        if command.errors:
            return ToolResult.fail("❌ " + "\n".join(command.errors) + "\n" + USAGE, "invalid_arguments")
        if command.action == "help":
            return ToolResult.ok(USAGE)
        session = self.session(str(guild.id))
        async with session.lock:
            return await self._run(command, session, voice_channel)

    async def _join(self, session: Session, voice_channel) -> ToolResult | None:
        if voice_channel is None:
            return ToolResult.fail("You need to be in a voice channel first!", "not_in_voice")
        problem = self.voice_problem()
        if problem:
            return ToolResult.fail(f"⚠️ Voice playback is unavailable: {problem}", "voice_unavailable")
        try:
            await self._connect(session, voice_channel)
        except (discord.ClientException, RuntimeError, asyncio.TimeoutError, discord.HTTPException) as error:
            return ToolResult.fail(f"Could not join {getattr(voice_channel, 'name', 'the voice channel')}: "
                                   f"{type(error).__name__}: {error}"[:300], "voice_connect_failed", retryable=True)
        return None

    async def _run(self, command: MusicCommand, session: Session, voice_channel) -> ToolResult:
        action = command.action
        if action == "status":
            return ToolResult.ok(self.describe(session), data={"index": session.index + 1,
                                                               "title": (session.current() or {}).get("title", ""),
                                                               "connected": session.connected()})
        if action == "queue":
            return ToolResult.ok(self.listing(session))
        if action == "youtube":
            return await self._youtube(command, session, voice_channel)
        if action == "init":
            failure = await self._join(session, voice_channel)
            if failure:
                return failure
            name = getattr(voice_channel, "name", "voice")
            if not session.tracks:
                return ToolResult.ok(f"Music player initialized in {name}, but playlist is empty")
            if not (session.voice.is_playing() or session.voice.is_paused()):
                error = await self._play_current(session, skip_failures=True)
                if error:
                    return ToolResult.fail(f"Music player initialized in {name}, but {error}", "play_failed")
            return ToolResult.ok(f"Music player initialized in {name}")
        if action == "play":
            if not session.connected():
                failure = await self._join(session, voice_channel)
                if failure:
                    return failure
            if session.voice.is_paused():
                session.voice.resume()
            elif not session.voice.is_playing():
                error = await self._play_current(session, skip_failures=True)
                if error:
                    return ToolResult.fail(error, "play_failed")
            return ToolResult.ok("Music player started")
        if not session.connected():
            return ToolResult.fail("Music player is not initialized. Use `$music init` first", "not_initialized")
        if action == "pause":
            if session.voice.is_playing():
                session.voice.pause()
            return ToolResult.ok("Music player paused")
        if action == "stop":
            session.generation += 1
            voice, session.voice = session.voice, None
            voice.stop()
            await voice.disconnect()
            self._save(session)
            return ToolResult.ok("Music player stopped")
        if not session.tracks:
            return ToolResult.fail("Playlist is empty", "empty_playlist")
        step = 1 if action == "next" else -1
        session.index = (session.index + step) % len(session.tracks)
        error = await self._play_current(session, skip_failures=True)
        if error:
            return ToolResult.fail(error, "play_failed")
        which = "next" if step == 1 else "previous"
        return ToolResult.ok(f"Music player is now playing the {which} song: {session.current()['title']}")

    async def _youtube(self, command: MusicCommand, session: Session, voice_channel) -> ToolResult:
        if not command.queue_only:
            failure = None if session.connected() else await self._join(session, voice_channel)
            if failure:
                return failure
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            return ToolResult.fail("yt-dlp is not installed (pip install yt-dlp).", "missing_dependency")
        added, problems = [], []
        for number, url in enumerate(command.urls, 1):
            try:
                info = await asyncio.to_thread(self.extract, url)
            except Exception as error:
                problems.append(f"⚠️ Failed to add URL {number}: {type(error).__name__}: {error}"[:300])
                continue
            if info.get("url"):
                self._streams[url] = (info["url"], time.monotonic())
            added.append({"source": url, "title": info.get("title") or url})
        if not added:
            return ToolResult.fail("\n".join([*problems, "❌ All URLs failed to load"]), "load_failed")
        position = session.index + 1 if session.tracks else 0
        session.tracks[position:position] = added
        del session.tracks[MAX_TRACKS:]
        titles = [track["title"] for track in added]
        if command.queue_only:
            self._save(session)
            if len(titles) == 1:
                text = f"➕ Added to queue: **{titles[0]}**"
            else:
                text = f"➕ Added {len(titles)} videos to queue:\n" + "\n".join(
                    f"{number}. {title}" for number, title in enumerate(titles, 1))
            return ToolResult.ok("\n".join([*problems, text]), data={"added": titles})
        session.index = position
        error = await self._play_current(session)
        if error:
            return ToolResult.fail("\n".join([*problems, error]), "play_failed")
        text = f"🎵 Now playing: **{titles[0]}**"
        if len(titles) > 1:
            text += f"\n➕ Added {len(titles) - 1} more to queue"
        return ToolResult.ok("\n".join([*problems, text]), data={"added": titles})
