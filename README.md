# Discord Study Bot

A powerful multi-functional Discord bot with unified AI chat interface (ChatGPT, Gemini, DeepSeek), music player, timer, reminders, and search capabilities.

这个Discord bot能够访问多个AI模型（ChatGPT、Gemini、DeepSeek）通过统一接口、支持Wolfram Alpha、Google搜索，并且包含音乐+计时器+提醒功能。

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

### 🔍 Search & Query
- **Google Search** (via SerpAPI) with professional CLI
- **Wolfram Alpha** computation & knowledge with CLI flags
- **Math Typesetting** (via WolframLanguage)

### 🎵 Music Player
- Play local music files with professional CLI commands
- Queue management with navigation
- Playback controls (play, pause, stop, next, previous)
- Persistent state across bot restarts

### ⏰ Time & Reminders
- M📋 Requirements
- Python 3.8 or higher
- Discord.py 2.x
- OpenAI Python SDK 1.x (for ChatGPT)
- Google Generative AI SDK (for Gemini)
- WolframLanguage installed (optional, for math queries)
- FFmpeg (optional, for music playback)

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
- **SerpAPI** (for Google search): [serpapi.com](https://serpapi.com) - 100 free searches/month
- **Wolfram Alpha**: [developer.wolframalpha.com](https://developer.wolframalpha.com)

### 3. Configure the Bot

#### Using config.yml (Recommended for Local Development)
1. Copy `config.yml.example` to `config.yml`
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

# Search APIs (optional)
SERP_API_KEY=...
WOLFRAM_APPID=...
WOLFRAM_PATH=/path/to/WolframKernel

# Discord Channel IDs (for clock/music features)
ID_CHANNEL=...
ID_CHANNEL1=...
ID_MESSAGE=...
ID_VOICECHANNEL=...
```

**Priority**: Environment variables override
SERP_API_KEY=...
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

## Usage

###📖 Usage

### Running the Bot
```bash
python main.py
```

### Quick Start Commands

```bash
$help                    # Show quick reference
$help chat              # Detailed help for unified chat
$help music             # Detailed help for music player
$help search            # Detailed help for search commands
$help time              # Detailed help for time/reminders
$help db                # Detailed help for database management
```

---

## 💬 Unified AI Chat Commands

The bot uses a **single `$chat` command** for all AI models with professional CLI-style flags.

### Basic Usage
```bash
$chat                              # Show current configuration
$chat -s <message>                 # Send message to active LLM
$chat --send "Your message here"   # Long form
```

### Switch LLM Provider
```bash
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

### Manage Prompts (Personalities)
```bash
$chat -p list                      # List all preset prompts
$chat -p show                      # Show current prompt
$chat -p 0                         # Set to Short Assistant (default)
$chat -p 2                         # Set to Misaka Minoto
$chat -p 3                         # Set to Saber
$chat -p set "Custom prompt"       # Set custom personality
$chat --prompt <action>            # Long form
```

**Built-in Prompts:**
- **0**: Short Assistant - Concise, helpful responses (4 sentences max)
- **1**: Blank - No personality constraints
- **2**: Misaka Minoto - From "A Certain Scientific Railgun"
- **3**: Saber - From "Fate" series

### Advanced Features
```bash
$chat -c                           # Clear chat history
$ch⚙️ Configuration Files

### llm_config.json
Contains AI prompts and fallback model lists:
- **prompts**: Array of personality presets
- **fallback_models**: Cached model lists with auto-update timestamps

### bot_data.db (SQLite)
Persistent storage for:
- Chat histories (per channel, per LLM)
- Channel settings (active LLM, model, listen mode)
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
$chat --listen                    # 切换监听模式（自动回复）
```

### 其他命令
```bash
$help                             # 查看全部命令
$help chat                        # 查看聊天系统详细帮助

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
- 📝 **DOCS**: Command examples file (COMMAND_EXAMPLES.md)

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
$chat --listen                     # Toggle ON/OFF
# Now just type normally:
Hello bot!                         # Bot will respond
How's the weather?                 # Bot will respond
$chat --listen                     # Turn OFF
```

**Note**: Each channel has its own LLM settings and chat history!

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
```

**Legacy format still supported**: `$music initialize`, `$music play`, etc.

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

**Note**: Requires SERP_API_KEY (100 free searches/month)

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

# No flags needed in listen mode
$chat --listen
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
- Ensure SERP_API_KEY is configured
- Free tier has 100 searches/month limit
- Get your key at [serpapi.com](https://serpapi.com)

## File Structure

```
DiscordStudyBot/
├── main.py                 # Main bot logic
├── chatGPTQuery.py         # OpenAI ChatGPT integration
├── geminiQuery.py          # Google Gemini integration
├── deepseekQuery.py        # DeepSeek integration
├── googleQuery.py          # Google search (SerpAPI)
├── wolframQuery.py         # Wolfram Alpha queries
├── ascii.py                # ASCII art for timer
├── config.yml              # Your config (DO NOT COMMIT)
├── config.example.yml      # Config template
├── requirements.txt        # Python dependencies
├── .gitignore             # Git ignore rules
├── musicList.txt          # Auto-generated music queue
└── music/                 # Your music files
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