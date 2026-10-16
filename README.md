# Discord Study Bot

A powerful multi-functional Discord bot with unified AI chat interface (ChatGPT, Gemini, DeepSeek), music player, timer, reminders, and search capabilities.

这个Discord bot能够访问多个AI模型（ChatGPT、Gemini、DeepSeek）通过统一接口、支持Wolfram Alpha、Google搜索，并且包含音乐+计时器+提醒功能。

## 🧱 Project Architecture

The project is now organized around a small app core plus adapter-driven feature modules.

- `main.py` - bootstrap only
- `app/` - application core and shared contracts
- `adapters/` - platform-specific adapters
- `features/` - command and passive behavior modules
- `services/` - shared infrastructure services
- `providers/` - external AI/search/media integrations
- `parsers/` - command parsing helpers
- `bridges/` - bridge base classes, examples, and bridge parser
- `assets/` - text and ASCII assets
- `docs/` - supplementary documentation

### Folder Layout

- `app/bot_app.py` - central application composition and dispatch
- `app/bot_types.py` - shared message/response contracts
- `adapters/discord_adapter.py` - Discord-specific message/event adapter
- `adapters/wechat_adapter.py` - Windows WeChat automation adapter via local pywechat
- `services/config_service.py` - config and environment loading
- `services/database.py` - persistent storage and channel state
- `providers/unified_chat.py` - shared LLM routing and model management
- `parsers/command_parsers.py` - CLI-style command parsing helpers
- `bridges/bridge_base.py` - abstract bridge contract
- `assets/help.txt` - help text content
- `features/feature_help.py` - help command handling
- `features/feature_chat.py` - unified AI chat handling
- `features/feature_search.py` - Google and Wolfram handlers
- `features/feature_database.py` - database command handling
- `features/feature_time.py` - time, clock, and reminder handling
- `features/feature_clip.py` - clip extraction handling
- `features/feature_music.py` - music and voice handling
- `features/feature_bridge.py` - bridge lifecycle and listen-mode handling
- `features/feature_broadcast.py` - broadcast command handling

### Design Goals

- Discord is treated as an interface layer rather than the bot itself.
- Features are isolated so they can be moved, replaced, or reused independently.
- The command router no longer depends on a monolithic `main.py` event body.
- Future forks can add another adapter without rewriting the whole bot.

### New core (in progress)

A replacement core with three extension slots (interfaces, engines, capabilities), a gateway, a
policy-enforcing manager, durable runs and owner approvals lives in `contracts/`, `core/` and
`extensions/`. Discord is an included extension. The new core does not change the running bot
(`main.py`) yet; the room engine, older features and WeChat are being ported as extensions. See
[core architecture](docs/core/ARCHITECTURE.md) and [writing extensions](docs/core/EXTENSIONS.md).

Quick start from a fresh clone:

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an
   application, add a bot, copy its token and enable **Message Content Intent** (Bot → Privileged
   Gateway Intents).
2. `pip install -r requirements.txt`, then create `.env` with `DISCORD_TOKEN=...` and at least one model
   key (`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` or `CLAUDE_API_KEY`).
3. `python -m core`. It prints an invite link if the bot is in no server yet; invite it.
4. DM the bot, or mention it (or reply to it) in a server channel. The application owner is the bot's
   owner: approvals (`$approve <id>`) arrive in their DM. `$help` lists commands, including `$video <url>`
   (needs ffmpeg), `$draw <prompt>` and `$status`; the assistant also uses them when asked.

`core.yml` is optional; copy [`core.example.yml`](core.example.yml) to change models, personas, which
channels it answers in, or to add extensions.

### WeChat Adapter Notes

- Run with `--mode wechat` to use the WeChat adapter.
- Run with `--mode discord` or no mode flag to use Discord.
- The WeChat adapter uses a local `pywechat/` clone and only works on Windows.
- `WECHAT_CHAT` is optional. If set, the adapter monitors that chat directly.
- If `WECHAT_CHAT` is empty, the adapter scans unread chats globally.
- `WECHAT_IDLE_CHAT` sets a known parking chat (default `File Transfer`) that the adapter returns to after scans and sends.
- `WECHAT_WHITELIST` is optional. If not empty, the bot only responds to those chat names.
- When `WECHAT_WHITELIST` is non-empty and `WECHAT_CHAT` is empty, the adapter polls each whitelisted chat directly and then returns to `WECHAT_IDLE_CHAT` instead of scanning every chat.
- WeChat requests are processed concurrently per chat, while outbound UI sends stay serialized for safety.
- `WECHAT_BROADCAST_CHAT` or `ID_CHANNEL1` can be used as the `$broadcast` target.
- Unsupported or limited on WeChat:
    - `$music` / voice playback
    - live clock message editing (`$start` / `$stop`)
    - Discord-style markdown/code-block rendering

## ✨ Features

### 🤖 Unified AI Chat System
- **Three LLM providers in one command**: ChatGPT, Gemini, DeepSeek
- **Professional CLI-style interface** with flags (`--send`, `--llm`, `--model`, `--prompt`)
- **Per-channel settings**: Each channel has its own active LLM, model, and chat history
- **Listen mode**: Auto-respond to all messages without command prefix
- **Customizable prompts**: 4 built-in personalities + custom prompt support
- **Dynamic model fetching**: Automatically updates available models from providers
- **Persistent fallback**: Models cached in config for offline reliability
- **Command chaining**: Set LLM, model, prompt, and send message in one command
- **Multi-provider image generation**: Use `$draw` with Gemini Nano Banana or OpenAI GPT Image 2.5

### 🔍 Search & Query
- **Keyless web search** with answer synthesis and clickable sources
- **Gemini grounding** for selective Google Search and Google Maps research in agent mode
- **Wolfram Alpha** computation & knowledge with CLI flags
- **Math Typesetting** (via WolframLanguage)

### 🎵 Music Player
- Play local music files with professional CLI commands
- **YouTube playback**: Stream audio from YouTube videos
- **Mixed playlists**: Combine local files and YouTube URLs
- Queue management with navigation
- Playback controls (play, pause, stop, next, previous)
- Persistent state across bot restarts

### 🌉 Bridge System (External App Integration)
- Named example and Linux WeChat account bridges
- Owner-only QR login, messages, explicit recipient sends and notifications
- Live reply whitelist, explicit group mentions, and independent notification subscriptions
- Independent per-chat assistants with a stable persona and configurable DeepSeek/Luna engine
- Extensible factories and persistent channel selection

### 🎬 Video Clip Extraction
- **Extract clips from 1000+ sites**: YouTube, Bilibili, Vimeo, TikTok, Twitter, Twitch, etc.
- **Smart size management**: Auto-detects Discord limits (10/50/100MB based on boost level)
- **Quality options**: Automatically suggests optimal settings to fit size limits
- **Multiple formats**: MP4, GIF, MP3, and more with format inference
- **Batch processing**: Queue multiple clips in one command
- **Two-phase workflow**: Preview size estimates before processing
- **No local storage**: Clips processed in temp files and auto-deleted

### ⏰ Time & Reminders
- M📋 Requirements
- Python 3.8 or higher
- Discord.py 2.x
- OpenAI Python SDK 1.x (for ChatGPT)
- Google Generative AI SDK (for Gemini)
- yt-dlp (for YouTube playback)
- WolframLanguage installed (optional, for math queries)
- FFmpeg (required for music playback)

## 🚀 Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Get API Keys

You'll need API keys for the features you want to use:

- **Discord Bot** (required): [Discord Developer Portal](https://discord.com/developers/applications)
- **OpenAI** (for ChatGPT): [platform.openai.com](https://platform.openai.com)
- **Google AI** (for Gemini): [ai.google.dev](https://ai.google.dev)
- **DeepSeek** (for DeepSeek): [platform.deepseek.com](https://platform.deepseek.com)
- **Wolfram Alpha**: [developer.wolframalpha.com](https://developer.wolframalpha.com)

### 3. Configure the Bot

#### Using config.yml (Recommended for Local Development)
1. Copy `config.example.yml` to `config.yml`
2. Fill in your API keys and Discord IDs
3. **⚠️ Never commit config.yml to git!** (Already protected by .gitignore)

#### Using Environment Variables (Recommended for Production/Deployment)
Set the following environment variables:

```bash
# Required
TOKEN=your_discord_bot_token

# AI APIs (configure the ones you want to use)
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
DEEPSEEK_API_KEY=...

# Computation API (optional)
WOLFRAM_APPID=...
WOLFRAM_PATH=/path/to/WolframKernel

# Discord Channel IDs (for clock/music features)
ID_CHANNEL=...
ID_CHANNEL1=...
ID_MESSAGE=...
ID_VOICECHANNEL=...
```

**Priority**: Environment variables override
WOLFRAM_APPID=...
WOLFRAM_PATH=C:/Program Files/Wolfram Research/Wolfram Engine/13.2/WolframKernel.exe

# Discord Channel IDs
ID_CHANNEL=...
ID_CHANNEL1=...
ID_MESSAGE=...
ID_VOICECHANNEL=...
```

The bot will prioritize environment variables over config.yml values.

### 4. Get Discord Channel IDs
- Enable Developer Mode in Discord (User Settings → Advanced → Developer Mode)
- Right-click channels/messages to copy IDs
- Add these IDs to your config.yml or environment variables:
  - `ID_CHANNEL` - Main text channel ID
  - `ID_CHANNEL1` - Secondary channel ID
  - `ID_MESSAGE` - Message ID for clock updates
  - `ID_VOICECHANNEL` - Voice channel ID for music

### 5. Restrict Discord access

`DISCORD_ALLOWED_CHANNELS` accepts one channel ID or a comma-separated list. If
it is omitted, the bot accepts commands in every channel it can access. The
legacy `ID_CHANNEL` setting is not an access-control setting. With
`DISCORD_REQUIRE_MENTION=true`, the bot ignores messages that do not mention it;
the setting defaults to `false`. Mentioned natural-language requests are routed
to the bounded agent.

```env
DISCORD_ALLOWED_CHANNELS=123456789012345678
DISCORD_REQUIRE_MENTION=false
TIMEZONE=America/Los_Angeles
AGENT_LOCALE=en-US
```

## Agent and persistent reminders

The agent can inspect a curated registry of bot actions. It cannot execute shell
commands, Python, or arbitrary command text generated by an LLM. Plans are
limited to five validated actions, and plans containing state changes require
confirmation.

```text
@Bot remind me tomorrow at this time to submit the report
@Bot show system status and then show the current time
@Bot search for tomorrow's weather, then remind me tomorrow at 8 PM to bring an umbrella
@Bot $agent tools
@Bot $agent confirm
@Bot $agent cancel
```

Reminders are stored in `bot_data.db`, survive restarts, and are delivered to
the channel where they were created. When `TIMEZONE` is omitted, the host's
local timezone is used. All four room members receive an authoritative
request-time context containing UTC, the configured local date and time, weekday, offset, IANA timezone,
and optional `AGENT_LOCALE`. Luna interprets reminder wording in the user's language into a typed relative
duration or timezone-aware timestamp; scheduling code validates that structure without parsing language-specific
phrases. Relative schedules are anchored to the original request time, so a later `$agent confirm` does not move
that reference time.

### Command queue and creator controls

Discord commands are processed one at a time with at most five commands waiting.
Later user commands are dropped when the queue is full. Creator commands are
exempt from that cap and jump ahead of waiting user commands without evicting
them. Creator commands remain FIFO relative to other creator commands, and no
command interrupts one already running.

The creator is authenticated using `CREATOR_USER_ID`. `$help secret` shows
creator-only controls, including queue status and the optional creator-only debug
mode. `$admin queue clear` runs outside the normal queue and clears all waiting
commands without interrupting the active command. The private `CREATOR_PROMPT`
is injected only into authenticated creator chat requests and is not stored in
chat history.

## Usage

###📖 Usage

### Running the Bot
```bash
python main.py
python main.py --mode discord
python main.py --mode wechat
```

`main.py` now wires the application container to the adapter selected by `--mode`.

### Quick Start Commands

```bash
$help                    # Show quick reference
$help chat              # Detailed help for unified chat
$help chat 2            # Open page 2 when a help topic has multiple pages
$help music             # Detailed help for music player
$help search            # Detailed help for search commands
$help time              # Detailed help for time/reminders
$help db                # Detailed help for database management
```

---

## 💬 Unified AI Chat Commands

The bot uses a **single `$chat` command** with two separate modes:

- **Agent mode (default)**: four characters share one conversation and work board. GPT-6 Luna,
  Gemini 3.8 Flash, Claude Haiku 5.5, and DeepSeek Flash can speak, use tools, or assign teammates
  focused steps. Explicit group invitations start independent replies concurrently. Work continues
  through the same loop, with visible updates and central approval for writes.
- **Role-play mode**: preserves the existing ChatGPT, Gemini, and DeepSeek providers, model
    selection, effort controls, prompt presets, and custom personas. Its histories remain separate
    from collaborative agent history.

Everyone may switch modes and customize provider, model, effort, or persona in role-play mode.
Agent team models, effort, and personalities are server-admin configured through environment variables.

### Basic Usage
```bash
$chat                              # Show current configuration
$chat -s <message>                 # Send message to active LLM
$chat --send "Your message here"   # Long form
$chat --trace -s <message>         # Show observable room progress
```

Characters usually speak in one to three sentences. A greeting and a server task use the same room loop:
there is no separate classifier, planner, task owner, reviewer, or model-generated recap. Each character
sees shared dialogue and verified results, while her diary remains private. Naming characters chooses the
initial speakers; `你们` or `you all` invites all four. Otherwise the initial speaker rotates. Independent
replies are sent as they finish, so a slow provider does not gate the other characters.

A character can assign a teammate a focused job. Independent jobs run concurrently; jobs with prerequisites
wait for those specific results. Tool failures stop dependent work, while independent jobs can continue.
The room retains pending work for a user answer or `$agent confirm`; `$agent cancel` drops it. Writes
require approval of concrete arguments. Duplicate successful preparations/writes are not executed twice.
Local handlers and allowed MCP capabilities use the same execution path.

Work updates show observable steps and results and remain visible without `--trace`. Short character-voiced work notes explain the relevant facts or constraints before an action. These are public explanations, not private chain-of-thought; ordinary chat can omit them. Tools can publish
stage and elapsed-time updates. Startup notes for ordinary conversation are removed. The defaults are
24 character steps and 24 tool actions across all characters and resumes; reaching a limit reports unfinished
work. First turns use low effort, follow-ups with results/dependencies use medium. Each native request has
a 35-second timeout and no transport retries; these limits do not guarantee total response time.
`[chat timing]` logs in rotating `chat_latency.log` measure queue wait, SDK attempts, token usage, tools,
and first visible delivery. See [architecture and diagram](docs/ARCHITECTURE.md),
[model defaults and prices](docs/MODELS.md), and [debugging](docs/BOT_DEBUGGING.md).

Attachments are persistently indexed per channel, separately from chat text. Newly shared files are fetched
once and passed to the characters as common input preparation. Older files can be inspected with
`read_attachment` by ID, comma-separated IDs, filename, or `latest`. Cached bytes expire after two hours,
with a 100 MiB per-channel limit. Supported images are passed to every participating character. Text,
source, JSON, CSV, Markdown, DOCX, and text-bearing PDFs are extracted as untrusted evidence. Explicitly
clearing history also clears the attachment index/cache. Source-dependent requests use registered retrieval
tools and verified evidence; the room must not claim a tool ran before it actually did.

Public webpage links sent through `$chat -s`, `$agent`, a bot mention, or listen mode are read as
untrusted evidence. A bare link is summarized; a link with a question is used to answer that
question. Multiple links are fetched together and produce one answer with sources. Text-bearing public PDFs are
downloaded with a 20 MB limit and extracted locally. Links discovered in search results or document index pages remain
available on the shared work board, allowing source-specific tasks to continue from search to the actual document
or section before the team answers. Local/private-network URLs, oversized pages, and unsupported binaries are blocked.

Tool use is conservative: ordinary conversation, writing, advice, coding help, and stable facts are
answered directly. Web search is reserved for current information, explicit research/source requests,
or facts too obscure to answer responsibly without retrieval. A URL used only as a citation or example
is not automatically fetched. Sourced answers keep clickable citations while suppressing Discord's
large link-preview cards.

Gemini grounding is selected when a character requests a registered grounding capability. Google Maps grounding
is currently English-only, so location queries are translated for grounding while the final answer
follows the user's language. If Google Search grounding is unavailable or quota-limited, the agent
falls back to keyless web retrieval and still asks Luna to synthesize the final answer.

### Character diaries

Each character keeps selective diary notes about the current user: explicitly shared facts, preferences, goals,
meaningful episodes, and personal reflections on actual exchanges. Notes are short paraphrases in the character's
own voice rather than copied messages. Characters normally save nothing unless a detail could help a future
conversation. Stable keys let a corrected fact replace an older note, while separate episode/reflection entries
preserve meaningful moments. Relevant notes and important facts are recalled before the character's next turn.

Diaries live in the `character_diary` table in `bot_data.db`, keyed by platform, room, user ID, character, and memory
key. Each character receives only her own notes about that user in that room. Participating characters decide on
updates during normal response generation; there is no additional planner, summarizer, or background AI request.
Validated hidden updates are removed before chat delivery and conversation-history persistence. Existing recent
chat history and compressed context remain separate from the diaries; `$chat --clear` does not erase diary notes.
The WeChat adapter currently identifies an author by chat, so its diary scope follows that chat identity.

```text
$memory DeepSeek
$memory Luna
$memory forget DeepSeek 12
$memory clear Luna
```

These commands only access the caller's notes in the current room. See [diary behavior and checks](docs/DIARY_MEMORY.md).

### Switch Mode
```bash
$chat --mode agent                  # Fixed four-assistant collaboration (default)
$chat --mode roleplay               # Provider and persona customization
```

Mode changes are available to everyone. Agent team settings are controlled by the server administrator.

### Configure Role-play Mode
```bash
$chat --mode roleplay
$chat -l chatgpt                   # Switch to ChatGPT
$chat -l gemini                    # Switch to Gemini  
$chat -l deepseek                  # Switch to DeepSeek
$chat --llm <provider>             # Long form
```

### Switch Model
```bash
$chat -m gpt-4                     # Switch to GPT-4
$chat -m gemini-1.5-pro            # Switch to Gemini Pro
$chat --model <model-name>         # Long form
$chat --models                     # List all available models
```

Provider, model, and effort changes are available to everyone in role-play mode and are rejected in
agent mode. DeepSeek uses the unified `deepseek-flash` model. Luna, Gemini, and Claude can be overridden with
`AGENT_LUNA_MODEL`, `AGENT_GEMINI_MODEL`, and `AGENT_CLAUDE_MODEL` in the service environment.
The defaults are GPT-6 Luna, Gemini 3.8 Flash, and Claude Haiku 5.5. Existing explicit
overrides remain authoritative. See [model choices and official pricing](docs/MODELS.md).

`AGENT_CONVERSATION_EFFORT` overrides room effort for all characters. Leave it unset for low initial
turns and medium turns inspecting tool results/dependencies. `AGENT_WORK_MAX_STEPS` and
`AGENT_WORK_MAX_ACTIONS` bound the shared run. `AGENT_GEMINI_MIN_INTERVAL_SECONDS` defaults to 1.1
seconds. The phase/per-character effort settings retained in `.env.example` apply to legacy helper calls.
Haiku 5.5 uses adaptive thinking with native effort settings; an explicitly pinned older Haiku continues
using its legacy request format. Provider thinking reserves are separate from the visible response budget.
Live answers are split at natural boundaries before Discord's 2,000-character message limit.

Discord output includes a rich math presentation pass. Short `$...$` or `\(...\)` expressions are converted to
readable Unicode where possible, while `$$...$$` and `\[...\]` display equations are rendered locally as embedded
PNG images with Matplotlib MathText. This does not require a system LaTeX installation, and code fences are left
untouched. Every equation uses the same fixed-width canvas, DPI, and font size so Discord applies a consistent visual
scale; only the height grows for explicit multiline equations. Configure these with `RICH_CONTENT_MATH_WIDTH_PX`,
`RICH_CONTENT_MATH_DPI`, `RICH_CONTENT_MATH_FONT_SIZE_PT`, and `RICH_CONTENT_MATH_MIN_HEIGHT_PX`. Discord Components
v2 does not expose author-controlled media dimensions or DPI, so this consistency is enforced in the PNG itself.
The original LaTeX remains in the shared transcript so later agents retain the exact mathematical form.
The team can also request one relevant supplementary visual: `image_search` locates and attributes an existing image,
while `generate_image` creates a new illustration. Downloaded or generated images are normalized, embedded in response
order, and added to the active shared vision context before later teammates speak.

`$draw <description>` uses the independent draw configuration. Gemini is the default provider with
`gemini-2.5-flash-image` (Nano Banana), while OpenAI defaults to `gpt-image-2.5-sunburst`. Select a request explicitly
with `$draw -p openai -m gpt-image-2.5-flare <description>` or `$draw -p gemini -m <model> <description>`. Use
`$draw --models` to merge live provider catalogs with the verified current model set, and `$draw --status` for
configured defaults. Attached images and images on the replied-to message are passed as visual references.

Configure this subsystem with `DRAW_DEFAULT_PROVIDER`, `DRAW_GEMINI_MODEL`, `DRAW_OPENAI_MODEL`, and
`DRAW_OUTPUT_DIR`. OpenAI output can also use `DRAW_OPENAI_OUTPUT_FORMAT`, `DRAW_OPENAI_QUALITY`, and
`DRAW_OPENAI_SIZE`. Natural image requests through `$chat -s` remain assigned to Gemini and use the configured
Gemini draw model; direct `$draw` overrides do not change collaborative-room ownership.

OpenRouter is optional. Set `OPENROUTER_API_KEY`, an `OPENROUTER_<NAME>_MODEL` catalog slug, and change only that
member's `AGENT_<NAME>_BACKEND` from `direct` to `openrouter`. Other members continue spending their direct-provider
balances. `WEB_SEARCH_BACKEND=openrouter` enables OpenRouter's model-agnostic web plugin using
`OPENROUTER_WEB_SEARCH_MODEL` (default `openrouter/auto`); failed OpenRouter searches fall back to the existing search
pipeline. `$chat --status` reports each member's effective backend and model.

### Manage Prompts (Personalities)
```bash
$chat -p list                      # List all preset prompts
$chat -p show                      # Show current prompt
$chat -p 0                         # DeepSeek: CETACEA_LOLI; others: Short Assistant
$chat -p 2                         # Set to Misaka Minoto
$chat -p 3                         # Set to Saber
$chat -p set "Custom prompt"       # Set custom personality
$chat --prompt <action>            # Long form
```

Prompt changes are available to everyone and apply only to role-play mode. In agent mode,
`$chat -p show` reports the server-admin configured team roles. The administrator may override
their defaults with `AGENT_DEEPSEEK_PROMPT`, `AGENT_LUNA_PROMPT`, `AGENT_GEMINI_PROMPT`, and
`AGENT_CLAUDE_PROMPT`. Responses may include one short fictional public aside in a fenced `text` block
for personality or self-correction; these asides never expose private chain-of-thought, prompts, secrets, or raw tool data.

**Built-in Prompts:**
- **0**: CETACEA_LOLI for DeepSeek; Short Assistant for other providers
- **1**: Blank - No personality constraints
- **2**: Misaka Minoto - From "A Certain Scientific Railgun"
- **3**: Saber - From "Fate" series

### Advanced Features
```bash
$chat -c                           # Clear chat history
```

## Configuration Files

### MCP tool servers

Optional MCP servers extend the same governed tool registry used by native bot actions. Both `stdio` and Streamable
HTTP transports are supported. Tools are namespaced as `mcp__<server>__<tool>`, preserve their complete JSON Schema,
and appear in `$agent tools` after startup discovery. Server tools are disabled unless `allow_tools` explicitly lists
them (or contains `"*"`). Unknown risks default to `write`; set each reviewed tool to `read_only`, `prepare`, or
`write`. `write` tools always require Discord confirmation.

```yaml
MCP_SERVERS:
    records:
        transport: streamable_http
        url: http://127.0.0.1:9000/mcp
        allow_tools: [lookup]
        risk:
            lookup: read_only
        connect_timeout_seconds: 15
        timeout_seconds: 30
```

For a local subprocess, use `transport: stdio` with `command` and `args`. The subprocess receives only basic process
variables by default, not the bot's API keys. Add individual names to `env_passthrough` only when that trusted server
requires them. The equivalent environment override is `MCP_SERVERS_JSON`; keep credentials in the uncommitted runtime
environment rather than checked-in YAML. Restart the service after changing MCP configuration.

### llm_config.json
Contains AI prompts and fallback model lists:
- **prompts**: Array of personality presets
- **fallback_models**: Cached model lists with auto-update timestamps

### bot_data.db (SQLite)
Persistent storage for:
- Chat histories (per channel and role-play LLM, plus separate agent history)
- Channel settings (active mode, role-play LLM/model/effort, listen mode)
- Music player state
- Reminder queue

**Backup**: Use `$db --export` to create JSON backups

---

## 🌏 中文命令参考 (Chinese Commands Reference)

### 统一AI聊天系统
```bash
$chat -s "你好"                    # 发送消息
$chat -l gemini                   # 切换到 Gemini
$chat -l chatgpt                  # 切换到 ChatGPT  
$chat -l deepseek                 # 切换到 DeepSeek
$chat -m gpt-4                    # 切换模型
$chat --models                    # 查看所有可用模型
$chat -p 2                        # 切换到 Misaka Minoto
$chat -p 3                        # 切换到 Saber
$chat -c                          # 清除聊天历史
$chat --listen on                 # 开启监听模式（自动回复）
```

### 其他命令
```bash
$help                             # 查看全部命令
$help chat                        # 查看聊天系统详细帮助
$help chat 2                      # 查看较长帮助主题的第 2 页

# 时间相关
$start                            # 开始时钟
$stop                             # 停止时钟
$time                             # 显示时间
$remindMeIn -t 30 -m "休息"       # 设置提醒

# 音乐播放
$music -i                         # 音乐启动/连接语音频道
$music -p                         # 播放/继续
$music --pause                    # 暂停
$music -n                         # 下一首
$music --prev                     # 上一首
$music --name                     # 显示歌名
$music -s                         # 停止并断开连接

# 查询功能
$wolfram -q "积分 x^2"            # 搜索 Wolfram Alpha
$google -s "Python 教程"          # 搜索 Google

# 数据库管理
$db -s                            # 查看数据库统计
$db -e                            # 导出数据库
$db -i                            # 导入数据库
```

### 重要说明
- 每个频道有独立的 LLM 设置和聊天历史
- 三个 AI（ChatGPT、Gemini、DeepSeek）的聊天历史互不干扰
- 监听模式下无需 `$` 前缀，直接输入即可
- 支持命令链：一条命令完成多个操作
- 模型列表自动从提供商API获取并更新

### 音乐设置
尽管试着让他尽可能的简单好用，但我发现那样的话要写的东西实在是太多了。这个程序会从一个叫 `musicList.txt` 的地方读取所有的音乐列表以及应该从哪一首开始播放，因此，如果你修改了文件夹里的文件最好直接删掉让程序重新生成这个文件。所有的音乐都应该放在一个叫做 `music` 的文件夹下面，而且没有 deep first search，所以只有在这个文件夹而非子文件夹中的文件才有效。

---

## 📜 Updates & Changelog

### 2026-02 Major Refactor
- ✨ **NEW**: Unified chat system - Single `$chat` command for all LLMs
- ✨ **NEW**: Professional CLI-style commands with flags (`--send`, `--llm`, `--model`)
- ✨ **NEW**: Command chaining support
- ✨ **NEW**: Listen mode for auto-responses
- ✨ **NEW**: Per-channel LLM/model settings
- ✨ **NEW**: Dynamic model fetching from provider APIs
- ✨ **NEW**: Persistent fallback model cache (`llm_config.json`)
- ✨ **NEW**: SQLite database for chat history and state
- ✨ **NEW**: Two-tier help system (`$help` vs `$help <topic>`)
- ✨ **NEW**: Quote escape support (mixed quotes, escaped quotes)
- ✨ **NEW**: Database export/import commands
- ✨ **NEW**: Improved UX messages (prompt names, history clearing notices)
- 🔧 **IMPROVED**: All commands now support professional CLI format
- 🔧 **IMPROVED**: Backward compatibility with legacy command formats
- 🔧 **IMPROVED**: Music player state persistence
- 🔧 **IMPROVED**: Better error handling and validation
- 🔐 **SECURITY**: Environment variable support for API keys
- 🔐 **SECURITY**: config.yml protection (.gitignore)
- 📝 **DOCS**: Comprehensive README with examples
- 📝 **DOCS**: Command examples file (`docs/COMMAND_EXAMPLES.md`)

### Previous Updates (2024-2026)
- Added Google Gemini support
- Added DeepSeek support
- Fixed Google search with SerpAPI
- Updated to Discord.py 2.x
- Updated to OpenAI SDK 1.x

---

## 📄 License

This is a personal project. Use at your own discretion.

---

## 🤝 Contributing

This is a personal bot project, but feel free to fork and modify for your own use!
1. Export current data: `$db --export`
2. Stop bot
3. Delete `bot_data.db`
4. Restart bot (recreates database)
5. Import if needed: `$db --import`

---
$chat -l chatgpt -m gpt-4 -p 3 -s "Are you my master?"
```

### Listen Mode
When enabled, the bot auto-responds to ALL messages (no `$` needed):

```bash
$chat --listen on                  # Enable listen mode
# Now just type normally:
Hello bot!                         # Bot will respond
How's the weather?                 # Bot will respond
$chat --listen off                 # Disable listen mode
```

**Note**: Each channel has its own LLM settings and chat history!

Chat history uses rolling context compression instead of a fixed message-count cutoff. When a
provider approaches 80% of its usable context window, older turns are summarized into durable
memory while recent turns remain verbatim. `$chat --clear` removes both raw history and the
compressed summary for the active provider.

---

## 🎵 Music Player Commands

Professional CLI with short and long flags:

```bash
$music -i                          # Initialize/connect to voice channel
$music --init                      # Long form

$music -p                          # Play/resume
$music --play                      # Long form

$music --pause                     # Pause playback

$music -s                          # Stop and disconnect
$music --stop                      # Long form

$music -n                          # Next song
$music --next                      # Long form

$music --prev                      # Previous song
$music --previous                  # Long form

$music --name                      # Show current song name

# YouTube Playback
$music -y "https://youtube.com/watch?v=xxx"           # Play immediately (default)
$music --youtube "video_url"                          # Long form
$music -y "url1" "url2" "url3"                       # Add multiple, play first
$music -y --queue "url1" "url2"                      # Add multiple to queue
$music -y "url" --add-next                            # Alternative flag
```

**YouTube Support**:
- **Instant playback**: `-y "url"` inserts after current song and skips to it immediately
- **Multiple URLs**: `-y "url1" "url2" "url3"` adds all URLs, plays the first one
- **Queue mode**: `-y --queue "url1" "url2"` adds all to queue without interrupting current song
- **Runtime playlist**: YouTube URLs from `-y` command are temporary (not saved to file)
- **Persistent playlist**: Add YouTube URLs to `musicList.txt` for permanent storage
- Mix local files and YouTube URLs in the same playlist
- Example `musicList.txt`:
  ```
  Soft Piano Music.mp3
  https://www.youtube.com/watch?v=dQw4w9WgXcQ
  Another Song.mp3
  https://youtu.be/jNQXAC9IVRw
  ```

**Behavior Examples**:
```bash
# Currently playing song #2
$music -y "url"                  # Inserts as #3, skips to it immediately
$music -y "url1" "url2" "url3"  # Inserts as #3,#4,#5, plays #3
$music -y --queue "url"          # Inserts as #3, continues playing #2
$music -y --queue "url1" "url2" # Inserts as #3,#4, continues playing #2

**Legacy format still supported**: `$music initialize`, `$music play`, etc.

---

## 🌉 Bridge System (External App Integration)

Named bridges connect a Discord channel to an external application. The runnable `example` bridge
is a local echo demo; the Linux `wechat` bridge provides owner-only account status, QR login,
conversation reads, explicit recipient sends, notifications, and independently configured DeepSeek
reply agents.

Every enabled WeChat assistant uses compatible registered tools through the shared agent loop. It speaks
before acting, reports progress in the Discord control channel, and verifies attachment delivery. Writes
require exact owner approval; contacts cannot approve them.

```text
$bridge example --init
$bridge example --send "hello"
$bridge wechat --status
$bridge wechat --login
$bridge wechat --chats
$bridge wechat --messages "Alice" --limit 10
$bridge wechat --send "Hello!" --to "Alice"
$bridge wechat --notify on --chat all --destination dm
$bridge wechat --whitelist list
$bridge wechat --whitelist add --chat "Alice" --kind direct
$bridge wechat --forward on --chat "Alice"
$bridge wechat --agent on --chat "Alice" --delay 20 --instructions "Keep replies brief."
$bridge wechat --agent off --chat "Alice"
$bridge wechat --disconnect
```

WeChat account commands require `WECHAT_BRIDGE_OWNER_ID` or `CREATOR_USER_ID`. Reply agents stay
off until a chat is whitelisted and explicitly enabled; groups additionally require an exact @mention.
Notifications are configured separately. Reply to a forwarded notification to reach its recorded origin.
`$bridge --help` and `$help bridge` share the parser’s live command catalogue. The Linux bridge supports
native database/WAL reads with stable message IDs and exact mention targets, plus the desktop accessibility
reader. Both use checked desktop sends for text and prepared attachments. Native setup requires private,
locally validated keys; it is separate from the Windows `--mode wechat` adapter.

See [WeChat bridge setup, architecture and limitations](docs/WECHAT_BRIDGE.md) for system packages,
login handling, per-chat settings, polling behavior and tests. Register additional implementations
with `BridgeFeature.register(name, factory)` and implement `BridgedObject`; the maintained example
shows the basic lifecycle. Personal bridge files remain ignored.

---

## 🎬 Video Clip Extraction Commands

Extract and share video clips from YouTube, Bilibili, and 1000+ other sites supported by yt-dlp.

### Agent Workflow

Mention the bot with a video request to use the iterative workflow. The agent first inspects the source and creates a
pending preview. If the result exceeds Discord's upload limit, Luna asks whether to trim a range, compress the whole
video to fit, or retain full quality on the bot host only when the original request did not already choose one of
those goals. If an adjustment meets the stated size and duration goal, the workflow advances directly to processing
approval instead of asking the same question again. Generated attachments are always sent to the active Discord
channel, so replies such as "send it here" or `请直接发送` do not need a recipient. `$agent confirm` remains available;
a natural-language reply also confirms the operation when its typed action exactly matches the pending write plan.
If Discord exhausts its attachment upload retries, the rendered file is retained on the bot host for recovery.

### Basic Usage

```bash
# Preview clip (recommended workflow)
$clip -u "video_url" -s 5 -e 15
→ Shows size estimate and quality options

# Start/end times are optional
$clip -u "url" -s 5          # From 5s to end of video
$clip -u "url" -e 30         # From beginning to 30s
$clip -u "url"               # Entire video (if fits limit)

# Confirm and process
$clip --confirm

# One-line force (skip preview)
$clip -u "url" -s 5 -e 15 --force
```

### Time Formats

Both formats supported:
```bash
$clip -u "url" -s 65 -e 80        # Seconds
$clip -u "url" -s 1:05 -e 1:20    # MM:SS format
$clip -u "url" -s 0:01:05 -e 0:01:20  # HH:MM:SS format
```

### Output Formats

Format inference based on extension:
```bash
$clip -u "url" -s 0 -e 10 --format mp4   # Video with audio (default)
$clip -u "url" -s 0 -e 10 --format gif   # Animated GIF (no audio)
$clip -u "url" -s 0 -e 10 --format mp3   # Audio only
$clip -u "url" -s 0 -e 10 --format webm  # WebM video
```

### Quality Control

Adjust settings to fit Discord size limits:
```bash
$clip --resolution 720p              # 1080p/720p/480p/360p
$clip --fps 30                       # Frame rate
$clip --bitrate 1500k                # Video bitrate
$clip --clip 2 --resolution 480p     # Adjust specific clip
```

### Multiple Clips

Process multiple clips in one command:
```bash
# Queue multiple clips
$clip -u "url1" -s 5 -e 15 -u "url2" -s 20 -e 30

# Preview shows all clips with size estimates
# Adjust individual clips:
$clip --clip 2 --resolution 720p

# Process all or skip specific ones:
$clip --confirm                 # Process all
$clip --confirm --skip 2        # Skip clip 2
$clip --keep-file              # Keep generated clip files after sending
$clip --delete-file            # Delete generated clip files after sending
```

### Two-Phase Workflow

**Phase 1: Preview**
```
$clip -u "bilibili_url" -s 10 -e 25

Bot shows:
📊 Clip Preview (Discord limit: 10MB)

Clip 1: ✅
Source: [Video Title] - Bilibili
Duration: 15.0s (0:10 → 0:25)
Selected: 720p @ 1500k (30fps)
Estimated: 8.2MB

Options:
  A) 1080p @ 2500k (30fps) → ~12.5MB ❌
  B) 720p @ 1500k (30fps) → ~8.2MB ✅
  C) 480p @ 1000k (30fps) → ~5.1MB ✅
  D) 360p @ 600k (30fps) → ~3.2MB ✅

Commands:
$clip --confirm - Process
$clip --clip 1 --resolution 480p - Adjust
$clip --keep-file - Keep generated file after send
$clip --cancel - Cancel

Generated clip files that are kept are stored under temp/clips/. You can clean up files older than one hour with:

python cleanup_old_clip_files.py --max-age-minutes 60
```

**Phase 2: Adjust & Confirm**
```bash
# If over limit, adjust quality
$clip --resolution 720p
→ Recalculates and shows new estimate

# Confirm when ready
$clip --confirm
→ Processes and uploads clip to Discord
```

### Size Limit Detection

Bot automatically detects server boost level:
- **No boost**: 10MB limit
- **Level 2 boost**: 50MB limit
- **Level 3 boost**: 100MB limit

Quality options are tailored to fit the detected limit.

### Supported Sites

Works with 1000+ sites including:
- YouTube
- Bilibili (哔哩哔哩)
- Vimeo
- Twitter/X
- TikTok
- Twitch
- SoundCloud
- Facebook
- Instagram
- Reddit
- And many more...

### Command Reference

```bash
# Clip specification
--url, -u <url>              Video URL (can specify multiple)
--start, -s <time>           Start time (seconds or MM:SS)
--end, -e <time>             End time (seconds or MM:SS)

# Quality settings
--resolution, -r <res>       Resolution (1080p/720p/480p/360p)
--fps <fps>                  Frames per second
--bitrate, -b <rate>         Video bitrate (e.g., 2500k, 1500k)
--format, -f <fmt>           Output format (mp4/gif/mp3/webm/etc.)

# Workflow control
--force                      Skip preview, process immediately
--confirm                    Process pending clips
--cancel                     Cancel pending clips
--clip <N>                   Modify specific clip (1-based index)
--skip <N>                   Skip clip N when confirming
```

### No Local Storage

- Clips are processed in temporary files
- Auto-deleted immediately after upload
- No permanent storage on bot server

---

## 🔍 Search & Query Commands

### Wolfram Alpha
```bash
$wolfram -q "integrate x^2"        # Query Wolfram Alpha
$wolfram --query "population of Tokyo"
$wolfram <text>                    # Legacy format (no flag)
```

### Google Search
```bash
$google -s "Python documentation"  # Search Google
$google --search "latest news"
$google -q "discord bot tutorial"  # Alternative flag
$google <text>                     # Legacy format (no flag)
```

No search-specific API key is required for `$google`. Agent mode can additionally use Gemini
Google Search grounding when `GEMINI_API_KEY` is configured, with automatic keyless fallback.

---

## ⏰ Time & Reminder Commands

### Time Display
```bash
$time                              # Show current time (Beijing, California, London)
$start                             # Start live updating clock
$stop                              # Stop clock/timer
```

### Reminders
```bash
$remindMeIn -t 30 -m "Review notes"       # Professional CLI
$remindMeIn --time 5 --message "Break"    # Long form
$remindMeIn 10 Take a break               # Legacy format
```

---

## 💾 Database Commands

Manage bot data and backups:

```bash
$db -s                             # View database statistics
$db --stats                        # Long form

$db -e                             # Export database to JSON
$db --export                       # Long form

$db -i                             # Import database from JSON
$db --import                       # Long form
```

**Legacy format still supported**: `$dbStats`, `$dbExport`, `$dbImport`

---

## 💡 Pro Tips

### Quote Handling
The command parser supports multiple quote styles:

```bash
# Mixed quotes (preferred for quotes inside text)
$chat -s 'He said "hello" to me'
$chat -s "It's working!"

# Escaped quotes
$chat -s "He said \"hello\""
$chat -s 'It\'s working'

# Enable listen mode
$chat --listen on
He said "hello" and I said "hi"    # Just type naturally!
```

### Multiple LLMs, Separate Histories
Each LLM (ChatGPT, Gemini, DeepSeek) maintains **separate chat history** per channel:
- Switch between LLMs freely
- Each remembers its own conversation
- Prompts are independent per LLM

### Model Auto-Updates
- Available models are fetched from provider APIs
- Cached for 1 hour (performance)
- Persistent fallback in `llm_config.json`
- Auto-updates every 7 days
- Manual refresh: `$chat --models`
- WolframLanguage must be installed on your system (for math queries)
- Never commit `config.yml` to git (it's protected by .gitignore)
- The bot uses `musicList.txt` to track the current song
- Songs must be placed in the `music/` folder (no subfolders scanned)
- If you modify songs in the music folder, delete `musicList.txt` to regenerate
- Each AI model maintains separate conversation history per Discord channel
- Conversation histories auto-expire after 10 hours of inactivity

## Music Setup

1. Create a `music/` folder in the project directory
2. Add MP3 files to the folder (no subfolders)
3. The bot will automatically create `musicList.txt` to track songs
4. If you modify songs, delete `musicList.txt` to regenerate

尽管试着让他尽可能的简单好用，但我发现那样的话要写的东西实在是太多了。这个程序会从一个叫musicList.txt的地方读取所有的音乐列表以及应该从哪一首开始播放，因此，如果你修改了文件夹里的文件最好直接删掉让程序重新生成这个文件。所有的音乐都应该放在一个叫做music的文件夹下面，而且没有deep first search，所以只有在这个文件夹而非子文件夹中的文件才有效。

## 中文命令参考 (Chinese Commands Reference)

对机器人发命令 (所有命令都用 $ 前缀)
```
$help查看全部

时间相关:
开始时钟 => $start
停止时钟 => $stop
印出时间 => $time
提醒 => $remindMeIn <minutes> <msg>

音乐播放:
音乐启动 => $music initialize
播放 => $music play
暂停 => $music pause
歌名 => $music name
下一首 => $music next
最后一首 => $music previous

查询功能:
显示数学 => $typeSetMath <equation>
搜索wolfram => $wolfram <query>
搜索Google => $google <query>

AI聊天:
ChatGPT => $chat <query>
Gemini => $gemini <query>
DeepSeek => $deepseek <query>
清除历史 => $chatClear / $geminiClear / $deepseekClear
更换人格 => $chatPrompt / $geminiPrompt / $deepseekPrompt <index>
```

## Troubleshooting

### Music not playing
- Ensure FFmpeg is installed and in your PATH
- Check that music files are directly in the `music/` folder (MP3 format)

### AI chat not working
- Verify the corresponding API key is configured
- Check API quota/billing status
- Review error messages in console

### Google search failing
- Verify outbound HTTPS and DNS access for the keyless search backends
- Check `GEMINI_API_KEY` quota when native Google grounding was expected
- Agent mode automatically falls back to keyless web retrieval when grounding is unavailable

## File Structure

```
DiscordStudyBot/
├── main.py                 # Bootstrap entrypoint
├── app/                    # Application core
├── adapters/               # Discord and WeChat adapters
├── features/               # Feature modules
├── services/               # Config and database services
├── providers/              # AI/search/media integrations
├── parsers/                # Command parsing helpers
├── bridges/                # Bridge contracts and examples
├── assets/                 # Help text and ASCII assets
├── docs/                   # Extra documentation
├── config.yml              # Your config (DO NOT COMMIT)
├── config.example.yml      # Config template
├── requirements.txt        # Python dependencies
├── .gitignore              # Git ignore rules
├── musicList.txt           # Auto-generated music queue
└── music/                  # Your music files
```

## Updates (2026)
- ✨ **NEW**: Added Google Gemini support
- ✨ **NEW**: Added DeepSeek support
- 🔧 Fixed Google search with SerpAPI
- 🔧 Updated to Discord.py 2.x
- 🔧 Updated to OpenAI SDK 1.x
- 🔐 Added environment variable support
- 📝 Improved documentation

## Security Notes

⚠️ **IMPORTANT**:
- Never commit `config.yml` to version control
- Keep your API keys secure
- Use environment variables in production
- The `.gitignore` file protects `config.yml` by default

## License

This is a personal project. Use at your own discretion.
