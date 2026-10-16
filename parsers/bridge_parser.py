"""Platform-independent bridge commands with quoted recipient/message support."""
from dataclasses import dataclass, field
import shlex
from parsers.bridge_commands import COMMANDS, OPTIONS, MODES


@dataclass
class BridgeCommand:
    bridge_type: str | None = None
    action: str = "status"
    target: str = ""
    message: str = ""
    mode: str | None = None
    limit: int = 20
    instructions: str | None = None
    delay_seconds: float | None = None
    max_reply_chars: int | None = None
    task_id: str = ''
    chat_kind: str | None = None
    mention_aliases: list | None = None
    destination: str | None = None
    errors: list = field(default_factory=list)


def parse_bridge_command(text):
    cmd = BridgeCommand()
    try:
        tokens = shlex.split(text)
        if tokens and tokens[0] == "$bridge":
            tokens.pop(0)
        if tokens and not tokens[0].startswith("-"):
            cmd.bridge_type = tokens.pop(0).lower()
        actions = {flag: action for action, (flags, _, _) in COMMANDS.items() for flag in flags}
        values = OPTIONS
        chosen = None
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in actions:
                action = actions[token]
                if chosen is not None:
                    raise ValueError("Use one bridge action per command")
                cmd.action = chosen = action
                index += 1
                if action in MODES:
                    default, allowed = MODES[action]
                    if index < len(tokens) and not tokens[index].startswith("-"):
                        cmd.mode = tokens[index].lower()
                        index += 1
                    else:
                        cmd.mode = default
                    if cmd.mode not in allowed:
                        raise ValueError(f"{token} expects " + ', '.join(sorted(allowed)))
                elif action in {"messages", "send"}:
                    words = []
                    while index < len(tokens) and not tokens[index].startswith("--"):
                        words.append(tokens[index])
                        index += 1
                    if action == "messages":
                        cmd.target = " ".join(words)
                    else:
                        cmd.message = " ".join(words)
            elif token in values:
                if index + 1 >= len(tokens):
                    raise ValueError(f"{token} requires a value")
                key = values[token]
                value = tokens[index + 1]
                if key in {"limit", "max_reply_chars"}:
                    value = int(value)
                elif key == "delay_seconds":
                    value = float(value)
                if key == 'mention_aliases':
                    cmd.mention_aliases = (cmd.mention_aliases or []) + [value]
                else:
                    setattr(cmd, key, value)
                index += 2
            else:
                raise ValueError(f"Unknown bridge option: {token}")
        if not 1 <= cmd.limit <= 100:
            raise ValueError("--limit must be between 1 and 100")
        if cmd.action == "send" and not cmd.message:
            raise ValueError("--send requires a message")
        if cmd.action == 'approve' and not cmd.task_id:
            raise ValueError('--approve requires the exact --task ID shown in the owner approval notice')
        if cmd.chat_kind is not None and cmd.chat_kind not in {'group','direct','unknown'}:
            raise ValueError('--kind expects group, direct, or unknown')
        if (cmd.chat_kind is not None or cmd.mention_aliases is not None) and cmd.action not in {'agent','whitelist'}:
            raise ValueError('--kind and --mention belong to --whitelist or --agent')
        if cmd.destination is not None and (cmd.action != 'notify' or cmd.destination not in {'here','dm'}):
            raise ValueError('--destination expects here or dm on a notification command')
        if any(value is not None for value in (cmd.instructions, cmd.delay_seconds, cmd.max_reply_chars)) and cmd.action != "agent":
            raise ValueError("Reply settings belong to --agent")
    except ValueError as error:
        cmd.errors.append(str(error))
    return cmd
