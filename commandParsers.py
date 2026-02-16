"""
Professional command parsers for all bot commands
"""
from typing import Optional, List, Tuple, Dict
import re

# ==================== Helper Functions ====================

def _consume_value(tokens: List[str], start_idx: int) -> Tuple[Optional[str], int]:
    """
    Consume value(s) from tokens, supporting both:
    1. Quoted strings (already tokenized as single token)
    2. Multiple tokens until next flag
    
    Returns: (value_string, tokens_consumed)
    
    Examples:
        ['--send', 'hello', 'world', '--llm', 'chatgpt']
         start=1 -> ('hello world', 3)
        
        ['--send', 'hello world', '--llm']  # quoted
         start=1 -> ('hello world', 2)
    """
    if start_idx >= len(tokens):
        return (None, 1)
    
    # Collect tokens until we hit another flag (starts with -)
    values = []
    consumed = 1
    
    for i in range(start_idx, len(tokens)):
        token = tokens[i]
        
        # Stop if we hit another flag
        if token.startswith('-') and len(token) > 1 and not token[1].isdigit():
            break
        
        values.append(token)
        consumed = i - start_idx + 2  # +2 because we consumed flag + value(s)
    
    if values:
        return (' '.join(values), consumed)
    else:
        return (None, 1)

def _tokenize(text: str) -> List[str]:
    """
    Tokenize command text, respecting quoted strings and escape sequences
    
    Supports:
    - Double quotes: --send "Hello world"
    - Single quotes: --send 'Hello world'
    - Mixed quotes: --send 'He said "hello"' or --send "It's working"
    - Escaped quotes: --send "He said \"hello\"" or --send 'It\'s working'
    - Escaped backslash: --send "Path: C:\\\\Users"
    
    Example:
        --send "Hello world" --llm chatgpt
        -> ['--send', 'Hello world', '--llm', 'chatgpt']
        
        --send "He said \"hello\" to me"
        -> ['--send', 'He said "hello" to me']
        
        --send 'She said "hi" and I said \'hey\''
        -> ['--send', 'She said "hi" and I said \'hey\'']
    """
    tokens = []
    current_token = ''
    in_quotes = False
    quote_char = None
    
    i = 0
    while i < len(text):
        char = text[i]
        
        # Handle escape sequences (backslash)
        if char == '\\' and in_quotes and i + 1 < len(text):
            next_char = text[i + 1]
            # Escape quote characters and backslash itself
            if next_char in ['"', "'", '\\']:
                current_token += next_char
                i += 2  # Skip both backslash and escaped char
                continue
        
        # Handle quotes
        if char in ['"', "'"]:
            if not in_quotes:
                # Start of quoted string
                in_quotes = True
                quote_char = char
                i += 1
                continue
            elif char == quote_char:
                # End of quoted string (only if it matches the opening quote)
                in_quotes = False
                quote_char = None
                if current_token or current_token == '':  # Preserve empty strings
                    tokens.append(current_token)
                    current_token = ''
                i += 1
                continue
            else:
                # Different quote type inside quoted string - treat as literal
                current_token += char
                i += 1
                continue
        
        # Handle spaces (token separators when not in quotes)
        if char.isspace() and not in_quotes:
            if current_token:
                tokens.append(current_token)
                current_token = ''
            i += 1
            continue
        
        # Add character to current token
        current_token += char
        i += 1
    
    # Add final token
    if current_token:
        tokens.append(current_token)
    
    return tokens

# ==================== Chat Command ====================

class ChatCommand:
    """Parsed chat command with all flags and values"""
    
    def __init__(self):
        self.llm: Optional[str] = None
        self.model: Optional[str] = None
        self.prompt_action: Optional[str] = None  # 'list', 'show', 'set', or index number
        self.prompt_value: Optional[str] = None   # For 'set' action
        self.message: Optional[str] = None
        self.show_models: bool = False
        self.show_status: bool = False
        self.clear_history: bool = False
        self.listen_mode: Optional[str] = None  # 'on', 'off', or None
        self.errors: List[str] = []
    
    def has_action(self) -> bool:
        """Check if command has any action to perform"""
        return (
            self.llm is not None or
            self.model is not None or
            self.prompt_action is not None or
            self.message is not None or
            self.show_models or
            self.show_status or
            self.clear_history or
            self.listen_mode is not None
        )

def parse_chat_command(command_text: str) -> ChatCommand:
    """
    Parse $chat command with professional CLI-style flags
    
    Supported flags:
        --llm, -l <name>           Set LLM (chatgpt/gemini/deepseek)
        --model, -m <name>         Set model for current/specified LLM
        --prompt, -p <action>      Prompt actions: list, show, set <text>, or index 0-3
        --send, -s <message>       Send message to LLM
        --models                   Show all available LLMs and models
        --status, -st              Show current configuration
        --clear, -c                Clear chat history
        --listen on/off            Enable or disable listen mode
    
    Examples:
        $chat --llm gemini --model gemini-1.5-pro --send Hello
        $chat -l chatgpt -m gpt-4 -p 2 -s Test message
        $chat --models
        $chat -p show
        $chat --clear --llm deepseek -s Fresh start
    """
    cmd = ChatCommand()
    
    # Remove the "$chat" prefix and trim
    text = command_text.strip()
    if text.startswith('$chat'):
        text = text[5:].strip()
    
    # If empty, default to showing status
    if not text:
        cmd.show_status = True
        return cmd
    
    # Tokenize the command (respecting quotes)
    tokens = _tokenize(text)
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        # LLM flag
        if token in ['--llm', '-l']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                cmd.llm = value.lower()
                i += consumed
            else:
                cmd.errors.append("--llm requires a value (chatgpt/gemini/deepseek)")
                i += 1
        
        # Model flag
        elif token in ['--model', '-m']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                cmd.model = value
                i += consumed
            else:
                cmd.errors.append("--model requires a model name")
                i += 1
        
        # Prompt flag
        elif token in ['--prompt', '-p']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                prompt_arg = value
                if prompt_arg in ['list', 'show']:
                    cmd.prompt_action = prompt_arg
                    i += consumed
                elif prompt_arg == 'set':
                    # Next value should be the prompt text
                    prompt_text, text_consumed = _consume_value(tokens, i + consumed)
                    if prompt_text:
                        cmd.prompt_action = 'set'
                        cmd.prompt_value = prompt_text
                        i += consumed + text_consumed - 1
                    else:
                        cmd.errors.append("--prompt set requires prompt text")
                        i += consumed
                elif prompt_arg.isdigit():
                    cmd.prompt_action = prompt_arg
                    i += consumed
                else:
                    cmd.errors.append(f"Invalid prompt action: {prompt_arg}")
                    i += consumed
            else:
                cmd.errors.append("--prompt requires an action (list/show/set/0-3)")
                i += 1
        
        # Send message flag
        elif token in ['--send', '-s']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                cmd.message = value
                i += consumed
            else:
                cmd.errors.append("--send requires a message")
                i += 1
        
        # Models list flag
        elif token == '--models':
            cmd.show_models = True
            i += 1
        
        # Status flag
        elif token in ['--status', '-st']:
            cmd.show_status = True
            i += 1
        
        # Clear flag
        elif token in ['--clear', '-c']:
            cmd.clear_history = True
            i += 1
        
        # Listen flag
        elif token == '--listen':
            value, consumed = _consume_value(tokens, i + 1)
            if value and value.lower() in ['on', 'off']:
                cmd.listen_mode = value.lower()
                i += consumed
            else:
                cmd.errors.append("--listen requires 'on' or 'off'")
                i += 1
        
        # Unknown flag
        else:
            cmd.errors.append(f"Unknown flag: {token}")
            i += 1
    
    # If no explicit action, show status
    if not cmd.has_action():
        cmd.show_status = True
    
    return cmd

# ==================== Music Command ====================

class MusicCommand:
    def __init__(self):
        self.action: Optional[str] = None  # init, play, pause, stop, next, prev, name, youtube
        self.youtube_urls: List[str] = []  # YouTube URLs to play/queue
        self.queue_only: bool = False  # If True, add to queue without playing
        self.errors: List[str] = []
    
    def has_action(self) -> bool:
        return self.action is not None

def parse_music_command(command_text: str) -> MusicCommand:
    """
    Parse $music command
    
    Flags:
        --init, --initialize, -i    Connect to voice channel
        --play, -p                  Resume playback
        --pause                     Pause playback
        --stop, -s                  Stop and disconnect
        --next, -n                  Next song
        --prev, --previous          Previous song
        --name                      Show current song
        --youtube, -y <url> [urls]  Play YouTube video(s) (default: skip to first)
        --queue, --add-next         With -y: add to queue without playing
    
    Examples:
        $music --init
        $music -p
        $music --next
        $music --youtube "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        $music -y "url1" "url2" "url3"           # Add multiple, play first
        $music -y --queue "url1" "url2" "url3"  # Add multiple to queue
    """
    cmd = MusicCommand()
    
    text = command_text.strip()
    if text.startswith('$music'):
        text = text[6:].strip()
    
    # Legacy support for old format
    if text and not text.startswith('-'):
        # Old format: $music initialize
        action_map = {
            'initialize': 'init',
            'play': 'play',
            'pause': 'pause',
            'stop': 'stop',
            'next': 'next',
            'previous': 'prev',
            'name': 'name',
            'playTest': 'playTest'
        }
        cmd.action = action_map.get(text.split()[0], text.split()[0])
        return cmd
    
    if not text:
        cmd.errors.append("No action specified")
        return cmd
    
    tokens = _tokenize(text)
    i = 0
    
    while i < len(tokens):
        token = tokens[i]
        
        if token in ['--init', '--initialize', '-i']:
            cmd.action = 'init'
            i += 1
        elif token in ['--play', '-p']:
            cmd.action = 'play'
            i += 1
        elif token == '--pause':
            cmd.action = 'pause'
            i += 1
        elif token in ['--stop', '-s']:
            cmd.action = 'stop'
            i += 1
        elif token in ['--next', '-n']:
            cmd.action = 'next'
            i += 1
        elif token in ['--prev', '--previous']:
            cmd.action = 'prev'
            i += 1
        elif token == '--name':
            cmd.action = 'name'
            i += 1
        elif token in ['--youtube', '-y']:
            cmd.action = 'youtube'
            i += 1
            # Consume all following tokens until we hit another flag
            while i < len(tokens) and not (tokens[i].startswith('-') and len(tokens[i]) > 1 and not tokens[i][1].isdigit()):
                cmd.youtube_urls.append(tokens[i])
                i += 1
            
            if not cmd.youtube_urls:
                cmd.errors.append("--youtube requires at least one URL")
        elif token in ['--queue', '--add-next']:
            cmd.queue_only = True
            i += 1
        else:
            cmd.errors.append(f"Unknown flag: {token}")
            i += 1
    
    return cmd

# ==================== Search Commands ====================

class SearchCommand:
    def __init__(self):
        self.query: Optional[str] = None
        self.errors: List[str] = []

def parse_wolfram_command(command_text: str) -> SearchCommand:
    """
    Parse $wolfram command
    
    Flags:
        --query, -q <text>    Search query
    
    Examples:
        $wolfram --query integrate x^2
        $wolfram -q "what is pi"
        $wolfram integrate x^2  (legacy)
    """
    cmd = SearchCommand()
    
    text = command_text.strip()
    if text.startswith('$wolfram'):
        text = text[8:].strip()
    
    if not text:
        cmd.errors.append("No query provided")
        return cmd
    
    # Legacy support - if no flags, treat entire text as query
    if not text.startswith('-'):
        cmd.query = text
        return cmd
    
    tokens = _tokenize(text)
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        if token in ['--query', '-q']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                cmd.query = value
                i += consumed
            else:
                cmd.errors.append("--query requires a search term")
                i += 1
        else:
            cmd.errors.append(f"Unknown flag: {token}")
            i += 1
    
    return cmd

def parse_google_command(command_text: str) -> SearchCommand:
    """
    Parse $google command
    
    Flags:
        --search, -s <text>    Search query
    
    Examples:
        $google --search python tutorial
        $google -s "how to code"
        $google python tutorial  (legacy)
    """
    cmd = SearchCommand()
    
    text = command_text.strip()
    if text.startswith('$google'):
        text = text[7:].strip()
    
    if not text:
        cmd.errors.append("No query provided")
        return cmd
    
    # Legacy support
    if not text.startswith('-'):
        cmd.query = text
        return cmd
    
    tokens = _tokenize(text)
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        if token in ['--search', '-s', '--query', '-q']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                cmd.query = value
                i += consumed
            else:
                cmd.errors.append("--search requires a query")
                i += 1
        else:
            cmd.errors.append(f"Unknown flag: {token}")
            i += 1
    
    return cmd

# ==================== Database Commands ====================

class DbCommand:
    def __init__(self):
        self.action: Optional[str] = None  # stats, export, import
        self.errors: List[str] = []

def parse_db_command(command_text: str) -> DbCommand:
    """
    Parse $db command
    
    Flags:
        --stats, -s        Show database statistics
        --export, -e       Export database to JSON
        --import, -i       Import database from JSON
    
    Examples:
        $db --stats
        $db -e
        $dbStats  (legacy)
    """
    cmd = DbCommand()
    
    text = command_text.strip()
    
    # Handle legacy commands
    if text == '$dbStats':
        cmd.action = 'stats'
        return cmd
    elif text == '$dbExport':
        cmd.action = 'export'
        return cmd
    elif text == '$dbImport':
        cmd.action = 'import'
        return cmd
    
    if text.startswith('$db'):
        text = text[3:].strip()
    
    if not text:
        cmd.errors.append("No action specified")
        return cmd
    
    tokens = _tokenize(text)
    
    for token in tokens:
        if token in ['--stats', '-s']:
            cmd.action = 'stats'
        elif token in ['--export', '-e']:
            cmd.action = 'export'
        elif token in ['--import', '-i']:
            cmd.action = 'import'
        else:
            cmd.errors.append(f"Unknown flag: {token}")
    
    return cmd

# ==================== Reminder Command ====================

class ReminderCommand:
    def __init__(self):
        self.minutes: float = 5.0
        self.message: Optional[str] = None
        self.errors: List[str] = []

def parse_reminder_command(command_text: str) -> ReminderCommand:
    """
    Parse $remindMeIn command
    
    Flags:
        --time, -t <minutes>    Time in minutes
        --message, -m <text>    Reminder message
    
    Examples:
        $remindMeIn --time 10 --message Take a break
        $remindMeIn -t 5 -m "Check the oven"
        $remindMeIn 10 Take a break  (legacy)
    """
    cmd = ReminderCommand()
    
    text = command_text.strip()
    if text.startswith('$remindMeIn'):
        text = text[11:].strip()
    
    if not text:
        return cmd
    
    # Legacy support: $remindMeIn 5 message here
    if not text.startswith('-'):
        parts = text.split(None, 1)
        try:
            cmd.minutes = float(parts[0])
            if len(parts) > 1:
                cmd.message = parts[1]
        except ValueError:
            cmd.errors.append("Invalid time format")
        return cmd
    
    tokens = _tokenize(text)
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        
        if token in ['--time', '-t']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                try:
                    cmd.minutes = float(value.split()[0])
                    i += consumed
                except ValueError:
                    cmd.errors.append("--time requires a number")
                    i += consumed
            else:
                cmd.errors.append("--time requires a value")
                i += 1
        elif token in ['--message', '-m']:
            value, consumed = _consume_value(tokens, i + 1)
            if value:
                cmd.message = value
                i += consumed
            else:
                cmd.errors.append("--message requires text")
                i += 1
        else:
            cmd.errors.append(f"Unknown flag: {token}")
            i += 1
    
    return cmd

# ==================== Clip Commands ====================

class ClipCommand:
    def __init__(self):
        self.urls: List[str] = []  # Can have multiple clips
        self.starts: List[str] = []  # Corresponding start times
        self.ends: List[str] = []    # Corresponding end times
        self.resolution: Optional[str] = None
        self.fps: Optional[int] = None
        self.bitrate: Optional[str] = None
        self.output_format: Optional[str] = None
        self.force: bool = False  # Skip preview, process immediately
        self.confirm: bool = False
        self.cancel: bool = False
        self.clip_index: Optional[int] = None  # For updating specific clip
        self.skip_indices: List[int] = []  # Clips to skip when confirming
        self.errors: List[str] = []

def parse_clip_command(command_text: str) -> ClipCommand:
    """
    Parse $clip command
    
    Flags:
        --url, -u <url>              Video URL (can specify multiple)
        --start, -s <time>           Start time (seconds or MM:SS)
        --end, -e <time>             End time (seconds or MM:SS)
        --resolution, -r <res>       Resolution (1080p, 720p, 480p, 360p)
        --fps <fps>                  Frames per second
        --bitrate, -b <rate>         Video bitrate (e.g., 2500k, 1500k)
        --format, -f <fmt>           Output format (mp4, gif, mp3, etc.)
        --force                      Skip preview, process immediately
        --confirm                    Confirm and process pending clips
        --cancel                     Cancel pending clips
        --clip <index>               Modify specific clip (1-based)
        --skip <index>               Skip clip when confirming
    
    Examples:
        $clip -u "url" -s 5 -e 15
        $clip -u "url" -s 1:05 -e 1:15 --format gif
        $clip -u "url1" -s 5 -e 15 -u "url2" -s 20 -e 30
        $clip --resolution 720p --clip 2
        $clip --confirm
        $clip --confirm --skip 2
        $clip -u "url" -s 5 -e 15 --force
    """
    cmd = ClipCommand()
    
    text = command_text.strip()
    if text.startswith('$clip'):
        text = text[5:].strip()
    
    if not text:
        cmd.errors.append("No parameters provided")
        return cmd
    
    tokens = _tokenize(text)
    i = 0
    
    while i < len(tokens):
        token = tokens[i]
        
        if token in ['--url', '-u']:
            if i + 1 < len(tokens):
                cmd.urls.append(tokens[i + 1])
                # Check if we have start/end times following
                i += 2
            else:
                cmd.errors.append("--url requires a URL")
                i += 1
        
        elif token in ['--start', '-s']:
            if i + 1 < len(tokens):
                cmd.starts.append(tokens[i + 1])
                i += 2
            else:
                cmd.errors.append("--start requires a time value")
                i += 1
        
        elif token in ['--end', '-e']:
            if i + 1 < len(tokens):
                cmd.ends.append(tokens[i + 1])
                i += 2
            else:
                cmd.errors.append("--end requires a time value")
                i += 1
        
        elif token in ['--resolution', '-r']:
            if i + 1 < len(tokens):
                cmd.resolution = tokens[i + 1]
                i += 2
            else:
                cmd.errors.append("--resolution requires a value")
                i += 1
        
        elif token == '--fps':
            if i + 1 < len(tokens):
                try:
                    cmd.fps = int(tokens[i + 1])
                    i += 2
                except ValueError:
                    cmd.errors.append("--fps requires a number")
                    i += 1
            else:
                cmd.errors.append("--fps requires a value")
                i += 1
        
        elif token in ['--bitrate', '-b']:
            if i + 1 < len(tokens):
                cmd.bitrate = tokens[i + 1]
                i += 2
            else:
                cmd.errors.append("--bitrate requires a value")
                i += 1
        
        elif token in ['--format', '-f']:
            if i + 1 < len(tokens):
                cmd.output_format = tokens[i + 1]
                i += 2
            else:
                cmd.errors.append("--format requires a value")
                i += 1
        
        elif token == '--force':
            cmd.force = True
            i += 1
        
        elif token == '--confirm':
            cmd.confirm = True
            i += 1
        
        elif token == '--cancel':
            cmd.cancel = True
            i += 1
        
        elif token == '--clip':
            if i + 1 < len(tokens):
                try:
                    cmd.clip_index = int(tokens[i + 1]) - 1  # Convert to 0-based
                    i += 2
                except ValueError:
                    cmd.errors.append("--clip requires a number")
                    i += 1
            else:
                cmd.errors.append("--clip requires an index")
                i += 1
        
        elif token == '--skip':
            if i + 1 < len(tokens):
                try:
                    cmd.skip_indices.append(int(tokens[i + 1]) - 1)  # Convert to 0-based
                    i += 2
                except ValueError:
                    cmd.errors.append("--skip requires a number")
                    i += 1
            else:
                cmd.errors.append("--skip requires an index")
                i += 1
        
        else:
            cmd.errors.append(f"Unknown flag: {token}")
            i += 1
    
    return cmd
