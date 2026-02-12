# Professional CLI Command System - Examples

## Chat Command - Full Flexibility

**Both quoted and unquoted multi-word values work:**

```bash
# Quoted strings
$chat --send "test message" --llm gemini
$chat -s "Hello world" -l chatgpt

# Unquoted (captures until next flag)
$chat --send test message --llm gemini
$chat -s Hello world from the bot -l deepseek
```

**Complex chained commands:**
```bash
# Change LLM, model, prompt, and send message in one command
$chat --llm gemini --model gemini-1.5-pro --prompt 2 --send Hello there

# Short form
$chat -l chatgpt -m gpt-4 -p 0 -s How are you doing?

# Clear history, switch to DeepSeek, set custom prompt, and chat
$chat --clear --llm deepseek --prompt set "You are a helpful coding assistant" --send Debug this code
```

**Status and information:**
```bash
$chat                    # Show current status
$chat --models           # List all LLMs and models
$chat --prompt list      # Show available presets
$chat --prompt show      # Show current prompt
$chat --status           # Explicit status
```

## Music Commands

**New professional format:**
```bash
$music --init            # or -i
$music --play            # or -p
$music --pause
$music --stop            # or -s
$music --next            # or -n
$music --prev
$music --name
```

**Legacy format still works:**
```bash
$music initialize
$music play
$music next
```

## Search Commands

**New professional format:**
```bash
# Wolfram
$wolfram --query integrate x^2 from 0 to 1
$wolfram -q "what is the speed of light"

# Google
$google --search python tutorial for beginners
$google -s "how to debug code"
```

**Legacy format still works:**
```bash
$wolfram integrate x^2
$google python tutorial
```

## Reminder Commands

**New professional format:**
```bash
$remindMeIn --time 10 --message Take a break
$remindMeIn -t 5 -m "Check the oven"
$remindMeIn -t 15 -m Meeting with team in 15 minutes
```

**Legacy format still works:**
```bash
$remindMeIn 10 Take a break
$remindMeIn 5
```

## Database Commands

**New professional format:**
```bash
$db --stats             # or -s
$db --export            # or -e
$db --import            # or -i
```

**Legacy format still works:**
```bash
$dbStats
$dbExport
$dbImport
```

## Key Features

✅ **Dual Value Capture:**
- Quoted strings: `--send "multi word message"`
- Unquoted: `--send multi word message` (captures until next flag)

✅ **Short and Long Forms:**
- `--llm` or `-l`
- `--model` or `-m`
- `--send` or `-s`
- `--query` or `-q`

✅ **Error Reporting:**
- Invalid flags are caught
- Missing required values are reported
- All errors shown before processing

✅ **Backward Compatible:**
- All old commands still work
- Gradual migration supported

✅ **Processing Order:**
Commands are processed in logical order regardless of how you write them:
1. LLM configuration
2. Model configuration
3. Prompt changes
4. Clear history
5. Listen mode
6. Send message
7. Show status/info
