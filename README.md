# Discord Study Bot

A multi-functional Discord bot with AI chat capabilities (ChatGPT, Gemini, DeepSeek), music player, timer, and various query tools.

这个Discord bot能够访问多个AI模型（ChatGPT、Gemini、DeepSeek）、Wolfram Alpha、Google，并且包含音乐+计时器+提醒功能。

## Features

### 🤖 Multiple AI Chat Models
- **ChatGPT** (OpenAI GPT-3.5/4)
- **Gemini** (Google's Gemini 2.0)
- **DeepSeek** (DeepSeek Chat)
- Per-channel conversation history
- Customizable AI personalities

### 🔍 Search & Query
- **Google Search** (via SerpAPI)
- **Wolfram Alpha** (computation & knowledge)
- **Math Typesetting** (via WolframLanguage)

### 🎵 Music Player
- Play local music files
- Queue management
- Playback controls

### ⏰ Time & Reminders
- Multi-timezone clock display
- Custom reminders
- ASCII art timer display

## Requirements
- Python 3.8 or higher
- Discord.py 2.x
- OpenAI Python SDK 1.x (for ChatGPT)
- Google Generative AI SDK (for Gemini)
- WolframLanguage installed (for math queries)
- FFmpeg (for music playback)

## Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Get API Keys

You'll need API keys for the features you want to use:

- **Discord Bot** (required): [Discord Developer Portal](https://discord.com/developers/applications)
- **OpenAI** (for ChatGPT): [platform.openai.com](https://platform.openai.com)
- **Gemini** (for Gemini): [ai.google.dev](https://ai.google.dev)
- **DeepSeek** (for DeepSeek): [platform.deepseek.com](https://platform.deepseek.com)
- **SerpAPI** (for Google search): [serpapi.com](https://serpapi.com) - 100 free searches/month
- **Wolfram Alpha**: [developer.wolframalpha.com](https://developer.wolframalpha.com)

### 3. Configure the Bot

You have two options for configuration:

#### Option A: Using config.yml (Local Development)
1. Copy `config.example.yml` to `config.yml`
2. Fill in your API keys and Discord IDs
3. **Never commit config.yml to git!**

#### Option B: Using Environment Variables (Recommended for Production)
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

### Running the Bot
```bash
python main.py
```

### Commands

All commands use the `$` prefix.

#### General
```
$help                          - Show all available commands
```

#### Time & Reminders
```
$time                          - Display current time in multiple timezones
$start                         - Start the clock display
$stop                          - Stop the clock/timer
$remindMeIn <minutes> <msg>    - Set a reminder
```

#### Music Player
```
$music initialize              - Initialize the music player
$music play                    - Resume playback
$music pause                   - Pause playback
$music name                    - Show current song
$music next                    - Skip to next song
$music previous                - Go to previous song
$music stop                    - Stop and disconnect
```

#### Search & Computation
```
$typeSetMath <equation>        - Evaluate math with Wolfram
$wolfram <query>               - Search Wolfram Alpha
$google <query>                - Search Google (requires SERP_API_KEY)
```

#### AI Chat Models
```
# ChatGPT
$chat <query>                  - Chat with ChatGPT
$chatClear                     - Clear ChatGPT history
$chatPrompt <index>            - Change ChatGPT personality (0-3)

# Gemini
$gemini <query>                - Chat with Gemini
$geminiClear                   - Clear Gemini history
$geminiPrompt <index>          - Change Gemini personality (0-3)

# DeepSeek
$deepseek <query>              - Chat with DeepSeek
$deepseekClear                 - Clear DeepSeek history
$deepseekPrompt <index>        - Change DeepSeek personality (0-3)
```

### AI Personalities

Use `$chatPrompt <index>`, `$geminiPrompt <index>`, or `$deepseekPrompt <index>`:

- **0**: Default (concise, helpful responses)
- **1**: Custom personality 1
- **2**: Misaka Minoto (A Certain Scientific Railgun)
- **3**: Saber (Fate series)

## Important Notes
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