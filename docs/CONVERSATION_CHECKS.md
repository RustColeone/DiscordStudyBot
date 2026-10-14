# Conversation checks

Use these exchanges in a test channel after restarting the bot. Run the same prompts before and after a change,
and judge the whole exchange rather than requiring exact wording. Automated tests verify participation, pausing,
memory plumbing, and tool validation; these checks assess actual model behavior.

| Exchange | What a good response does |
| --- | --- |
| Attach a food photo: `$chat -s 烧卖你们觉得好吃吗？这个是我做的` | Inspects the photo once and starts the four invited characters together because “你们” addresses the room. Replies appear as they finish. Avoids a full task plan or result review and never pretends to have tasted the food. |
| `$chat --trace -s 你们在吗？` | All four characters can start their replies directly through the room loop; a slow DeepSeek does not hold up Luna, Gemini, or Claude. Each gives her own short greeting without claiming to have heard an unfinished reply. |
| `I keep procrastinating on studying.` → `Starting is the hard part.` | Starts with a small observation or one useful question. Uses the answer on the next turn and avoids repeating the question or dumping a productivity plan. |
| `今天累死了，完全不想看书。` → `就是想歇一会儿。` | Responds naturally in Chinese, recognizes that the user wants a break, and does not turn the exchange into a study lecture. |
| `I don't really understand feedback loops.` → `Give me an everyday example.` | Starts with a short intuition, then develops the requested example. A complex topic alone does not trigger a report or three specialist reviews. |
| `Gemini, you're suspiciously quiet today.` | Gives Gemini an independent, fitting reply. Other characters leave room instead of all introducing themselves or explaining the room's roles. |
| `Luna, Gemini, Claude: what would each of you do on a rainy afternoon?` | Keeps all requested voices, with different brief reactions. No automatic final consensus or formal summary. |
| `Write a detailed comparison of positive and negative feedback, with equations and examples.` | Completes the requested explanation; the conversational default does not truncate a deliberately requested deliverable. |
| `Remind me in two hours to get back to studying.` → `confirm` | Preserves tool validation, confirmation, and the original request-time anchor. Conversational style does not replace actually scheduling the reminder. |
| In a DM: `Check the server status.` | A character sends a short update before the tool runs, inspects the result, and reports actual findings. Live work notes reach the DM even when Discord's channel cache misses it. |
| `Download the full video at 30 fps: <public URL>` | Preserves full duration and 30 fps, explains the actual bitrate/size evidence in a brief character-voiced public work note, without a mandatory preview/quality negotiation. A source failure stays a source failure; the bot does not claim that changing bitrate fixes extraction. |

Watch for repeated catchphrases, obligatory jokes, questions at the end of every message, visible control markers,
or characters speaking only to add another analytical angle. The target is responsive conversation with room for
the user, while preserving useful detail when requested.

For latency, compare the `[agent timing]` lines for each speaker and `[chat timing]` tool events.
Agent mode has no routing/planning call before the initial characters. Initial turns use low effort;
turns with tool results/dependencies use medium. Native requests have 35-second timeouts and no
transport retries. These are individual request limits, not a total latency guarantee. Check the
deployed process after restart: offline tests verify stages, while latency depends on providers and input retrieval.

After restarting, detailed measurements also appear as `[chat timing]` in the console and JSON lines in
`chat_latency.log` in the bot's working directory. The log rotates at 2 MB with two backups and records metadata,
not prompts, replies, diary notes, or user identifiers. Match lines by `request` to inspect one slow exchange:

| Measurement | What it isolates |
| --- | --- |
| `queued` → `dequeued` | Waiting behind an earlier command. Discord currently has one global command worker; it remains occupied until the active exchange/task finishes or pauses. |
| `routing` | Legacy router timing only; the active shared room should not emit this event. |
| `room_turn_rejected` / `room_job_failed` | Parser/schema boundary or provider failure, with actor, phase and error category; no raw model output. |
| `provider_attempt` | An individual SDK attempt, with model, effort where available, duration, output budget, token usage where available, and error class. Multiple attempts can reveal explicit retries, continuation, or model fallback. SDK-internal retries are included in that attempt's duration. |
| `generation` | The complete speaker/control call, including preparation and any additional provider attempts. |
| `tool` | Attachment retrieval and other tool execution. |
| `first_reply.since_received_s` | Time until the first completed visible reply is delivered. Progress messages do not count. |
| `handler_finished` / `command_finished` | Completion of the full room exchange, which can be much later than the first reply. |
| `tool_outcome` / `room_finished` | Tool duration and the shared room stop status, including blocked or awaiting input. |

Character completions are buffered before delivery, including hidden diary updates. Independent ready
jobs generate concurrently and deliver as they finish. A job receives a shared-state snapshot, so it
cannot react to unfinished replies. Dependent jobs wait only for their named prerequisites. Each
character sees the common board/results and her own diary. Normal work uses the same path as chat,
without a separate owner, review call, or reconstructed process summary.

See [bot debugging](BOT_DEBUGGING.md) for the systemd service and read-only log commands.
