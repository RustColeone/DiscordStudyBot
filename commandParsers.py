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
        self.toggle_listen: bool = False
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
            self.toggle_listen
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
        --listen                   Toggle listen mode
    
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
            cmd.toggle_listen = True
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
        self.action: Optional[str] = None  # init, play, pause, stop, next, prev, name
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
    
    Examples:
        $music --init
        $music -p
        $music --next
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
    
    for token in tokens:
        if token in ['--init', '--initialize', '-i']:
            cmd.action = 'init'
        elif token in ['--play', '-p']:
            cmd.action = 'play'
        elif token == '--pause':
            cmd.action = 'pause'
        elif token in ['--stop', '-s']:
            cmd.action = 'stop'
        elif token in ['--next', '-n']:
            cmd.action = 'next'
        elif token in ['--prev', '--previous']:
            cmd.action = 'prev'
        elif token == '--name':
            cmd.action = 'name'
        else:
            cmd.errors.append(f"Unknown flag: {token}")
    
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
