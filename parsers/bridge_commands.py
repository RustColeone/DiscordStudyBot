"""One command catalogue for bridge parsing and rendered help."""
COMMANDS = {
    'status': (('--status', '-st'), '--status', 'Show account and worker status.'),
    'init': (('--init', '-i'), '--init', 'Start the selected bridge.'),
    'disconnect': (('--disconnect', '-d'), '--disconnect', 'Pause the bridge without logging out.'),
    'login': (('--login',), '--login', 'Send the current WeChat login QR.'),
    'chats': (('--chats',), '--chats', 'List exposed conversations and IDs.'),
    'messages': (('--messages',), '--messages "chat" --limit 10', 'Read recent messages.'),
    'send': (('--send', '-s'), '--send "message" --to "chat"', 'Send one explicit owner message.'),
    'listen': (('--forward', '--listen', '-l'), '--forward on|off|status --chat "chat"', 'Bind ordinary Discord messages to one whitelisted WeChat chat.'),
    'notify': (('--notify',), '--notify on|off|status --chat "chat or all" --destination here|dm', 'Forward incoming notifications to this channel or the owner’s DM.'),
    'whitelist': (('--whitelist',), '--whitelist list|add|remove --chat "chat" --kind group|direct --mention "nickname"', 'Manage reply eligibility live; group replies require @mentions.'),
    'agent': (('--agent',), '--agent on|off|status --chat "chat"', 'Enable an assistant only for a whitelisted conversation.'),
    'approve': (('--approve',), '--approve --chat "chat" --task "exact ID"', 'Approve the exact retained work as the owner.'),
    'cancel_work': (('--cancel-work',), '--cancel-work --chat "chat"', 'Cancel a conversation’s pending work.'),
    'help': (('--help', '-h'), '--help', 'Show this command catalogue.'),
}
OPTIONS = {'--to':'target', '--chat':'target', '--limit':'limit', '--instructions':'instructions',
           '--delay':'delay_seconds', '--max-length':'max_reply_chars', '--task':'task_id',
           '--kind':'chat_kind', '--mention':'mention_aliases', '--destination':'destination'}
MODES = {'listen': ('toggle', {'on','off','status','toggle'}),
         'notify': ('status', {'on','off','status'}), 'agent': ('status', {'on','off','status'}),
         'whitelist': ('list', {'list','add','remove','defaults'})}


def bridge_help(bridges=('example', 'wechat')):
    lines = ['Bridge commands — quote names and messages containing spaces.',
             'Registered bridges: ' + ', '.join(bridges), 'Use $bridge <name> <action>. Named selection is remembered per Discord channel.', '']
    for _, syntax, description in COMMANDS.values():
        lines.extend(['$bridge wechat ' + syntax, description, ''])
    lines.extend(['$bridge wechat --whitelist defaults --mention "account nickname"',
                  'Repeat --mention for additional exact aliases; a group override replaces the account defaults.',
                  'Assistant options: --instructions "text" --delay 20 (minimum send interval) --max-length 500.',
                  'Whitelist membership gates automated outgoing messages. Notifications are configured separately.',
                  'Reply to a forwarded notification to send an owner reply to its recorded origin. Message IDs select the chat; copied text cannot redirect it.',
                  'WeChat controls are owner-only. Explicit --send is an owner action, not an automatic reply.',
                  'Example bridge: $bridge example --init, then --send "hello" or --listen on.'])
    return '\n'.join(lines)
