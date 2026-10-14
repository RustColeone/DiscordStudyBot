# Character model defaults

Verified against official provider documentation and the configured accounts on October 13, 2026.
Luna, Claude and DeepSeek use the latest budget families. Gemini uses regular 3.8 Flash by user preference.
Character identities and private diaries are independent of their underlying model assignments.

| Character | Default API model | Standard input / output per 1M tokens | Change |
| --- | --- | --- | --- |
| Luna | `gpt-6-luna` | $0.10 / $0.50 | Replaces GPT-5.6 Luna. |
| Claude | `claude-haiku-5-5` | $0.10 / $0.50 up to 100K input | Replaces Haiku 4.5; uses adaptive thinking and native effort. |
| DeepSeek | `deepseek-flash` | $0.15 / $0.60 off-peak; $0.30 / $1.20 peak | Same endpoint now serves V4.1 Flash. |
| Gemini | `gemini-3.8-flash` | $0.75 / $3.75 (introductory) | Keeps regular Flash by user preference. |

Sources: [OpenAI GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Claude Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview),
[DeepSeek pricing and model mapping](https://api-docs.deepseek.com/quick_start/pricing/),
[Google pricing](https://ai.google.dev/gemini-api/docs/pricing).

Gemini 3.8 Flash is the default. Its current introductory text price is $0.75 input / $3.75
output, scheduled to rise to $1.50 / $7.50 on January 1, 2027. Flash-Lite remains an optional
explicit override; the default does not select it to reduce cost. See
[Google's current model catalog](https://ai.google.dev/gemini-api/docs/models) and
[Google pricing](https://ai.google.dev/gemini-api/docs/pricing).

Prices exclude tool charges and cache discounts. Haiku requests above 100K input tokens cost
$0.50 / $2.50; GPT-6 Luna applies higher rates above 272K input. DeepSeek peak pricing applies
Monday–Friday 01:00–04:00 and 06:00–10:00 UTC except mainland Chinese public holidays.
The room uses bounded recent context and results; normal turns should remain far below these
long-context thresholds. Actual reasoning tokens can add to billed output.

Explicit `AGENT_*_MODEL` environment overrides are preserved. OpenRouter model slugs remain
independently configured; updating direct-provider defaults does not change those slugs. The existing
explicit Gemini fallback stays `gemini-3.6-flash`; it is only requested on unavailable-model errors.
Image generation and stored roleplay model selections remain separately configured.

Haiku 5.5 rejects the old `thinking: enabled` / `budget_tokens` request format. Its adapter uses
adaptive thinking, `output_config.effort`, and extra total token headroom so thinking does not exhaust
the visible-text budget. Explicitly pinned Haiku 4.5 continues using the older format. Text blocks are
selected by type and continuation preserves provider content blocks. See the
[official migration guide](https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide).

Direct API probes accepted all four default IDs and returned valid room-turn JSON. The tiny
compatibility prompts took roughly 1–3 seconds. Those measurements do not predict full-context
conversation latency or quality; the shared loop is separately tested with the actual character
prompts, tool schemas and local adapters. No probe sends Discord messages.
