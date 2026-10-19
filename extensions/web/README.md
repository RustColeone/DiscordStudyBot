# `web`: search, page reading, image search, maps and Wolfram|Alpha

`web` lets the bot look things up: search the web, read a public page or PDF, find one real image to
send with an answer, look up places with Google Maps, and calculate with Wolfram|Alpha. Every
capability is a `read`, so it runs without asking. Study it for calling outside services with keys
from `.env`, falling back from one service to the next, returning sources, keeping a key out of error
messages, and fetching untrusted URLs without reaching the bot's own network.

## What it adds

| Capability | Tier | Access | Command | Needs | Timeout |
| --- | --- | --- | --- | --- | --- |
| `web.search` | `read` | member | `$search <query>` | | 60 s |
| `web.read_page` | `read` | member | `$read <url>` | | 60 s |
| `web.image_search` | `read` | member | | `files` | 150 s |
| `web.maps` | `read` | member | | | 60 s |
| `web.wolfram` | `read` | member | `$wolfram <query>` | | 60 s |

None sets `private`, `never_auto` or `confirm`. The module has no other contributions.

| Setting | Kind | Default | Env |
| --- | --- | --- | --- |
| `wolfram_appid` | `secret` | | `WOLFRAM_APPID` |
| `searxng_url` | `text` | `""` | |
| `grounding` | `boolean` | `true` | |

## Setup

`config.example.yml` (copy it to `config.yml` next to it; `extension_config.web` in `core.yml`
overrides it):

```yaml
grounding: true                 # Google grounding through Gemini first
grounding_model: gemini-3.8-flash
searxng_url: ""                 # a SearXNG server, e.g. http://localhost:8080 (SEARXNG_URL in .env, the older way)
```

* `grounding`: try Google Search grounding through Gemini before the link backends.
* `grounding_model`: the Gemini model used for search and maps grounding.
* `searxng_url`: a SearXNG server (for example `http://localhost:8080`) with its JSON format enabled.
  Empty means `SEARXNG_URL` from the environment (the older way: `.env` is meant for secrets, and its
  values are blanked out of replies), or no SearXNG.

`.env` variables (names only; `python -m core setup` asks for `WOLFRAM_APPID`):

| Variable | Used by |
| --- | --- |
| `GEMINI_API_KEY` | Search grounding and `web.maps` |
| `WOLFRAM_APPID` | `web.wolfram` (an AppID from developer.wolframalpha.com) |
| `SEARXNG_URL` | `web.search`, when `searxng_url` is empty (prefer `searxng_url`: it is a setting, not a secret) |

Without any of them, `$search` still works through DuckDuckGo, and `$read` and image search need no key.

Packages (all in [`requirements.txt`](../../requirements.txt)): `requests`, `google-genai`, `ddgs`
(DuckDuckGo search and image search), `pypdf` (PDFs), `pillow` (images).

To turn the module off:

```yaml
modules:
  exclude: [extensions/web]
```

## Using it

```
$search capital of france
$read https://example.com/article
$read <https://example.com/paper.pdf>
$wolfram integrate x^2
$wolfram -q "distance from earth to mars"
```

* `$search` replies with a grounded answer and its sources, or with up to 5 result links and snippets
  (a model may ask for 1 to 10).
* `$read` replies with the page's title, URL and text. Angle brackets around the URL are removed. A
  model may also ask for up to 100 of the page's links (`max_links`).
* `$wolfram` replies with Wolfram|Alpha's plaintext pods (`## Title` and text). The legacy
  `--query "…"` and `-q …` forms are accepted.
* `web.image_search` and `web.maps` have no command; the assistant uses them when an image would help
  or a question is about places ("cafes open late in Seattle").

Limits: pages up to 1 MB and 30,000 characters of text; PDFs up to 20 MB and 60,000 characters; HTML,
XHTML, plain text, JSON and PDF only; at most 3 redirects; only `http`/`https` on ports 80 and 443.
Images: 8 candidates, 12 MB and 60 s per download, 120 s overall, scaled to at most 1800 px.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | `Searcher` (grounding, SearXNG, DuckDuckGo), the five handlers, capabilities and settings |
| [`pages.py`](pages.py) | `validated_url` (public addresses only), `fetch` with checked redirects, HTML-to-text, PDFs |
| [`images.py`](images.py) | Image candidates from DuckDuckGo, safe download, normalizing with Pillow |
| [`wolfram.py`](wolfram.py) | The Wolfram\|Alpha full results API and legacy `$wolfram` flags |
| [`config.example.yml`](config.example.yml) | Documented settings |

`setup` builds one `Searcher` from `context.config`; the handlers close over it. Every outside call is
blocking (`requests`, the Gemini SDK, `ddgs`), so each handler runs it with `asyncio.to_thread`.

**Search fallback** (`Searcher.search`, `extension.py:87-101`):

1. If `grounding` is on and `GEMINI_API_KEY` is set: Gemini's Interactions API with the `google_search`
   tool (`interactions.create(..., tools=[{"type": "google_search"}], timeout=25.0)`). The answer text
   and the URLs in its annotations become the result (`backend: "google"`).
2. Otherwise, or if that fails or returns nothing: SearXNG's `/search?format=json`, if configured.
3. Then DuckDuckGo through `ddgs`.
4. If every backend fails, the result is `unavailable` with `retryable=True`, and the summary names
   each backend's error ("SearXNG: refused; DuckDuckGo: ratelimited").

The Gemini client is created on first use and cached, including its absence; a key added later needs a
restart.

**Reading a page** (`pages.fetch`): `validated_url` accepts only `http`/`https`, no user or password in
the URL, ports 80 and 443, no `localhost`, and only host names whose every resolved address is global
(`ipaddress.is_global`: no loopback, private, link-local or cloud metadata addresses). Redirects are
not followed by `requests`; each `Location` is validated the same way, up to 3 hops. The body is
streamed and stopped at the size limit. HTML goes through the standard library's `HTMLParser`, which
drops `script`, `style`, `noscript`, `svg` and `template` and collects the title and absolute
`http(s)` links. PDFs go through `pypdf`, page by page until the character limit. A `PageUnavailable`
fails as `unavailable`; a network error fails as `unavailable` with `retryable=True`.

**Image search** (`images.locate_image`): DuckDuckGo images (`safesearch="moderate"`), then for each
candidate a download through the same `validated_url` checks per hop (`images.download`), then
`save_image`: Pillow opens it (anything that is not an image is skipped), shrinks it to 1800 px, saves a
PNG in the run's workspace, and falls back to JPEG (quality 85) if the PNG is above the conversation's
upload limit. The first candidate that works is sent; the summary names its title and source page.

**Maps** (`maps`): the same grounding call with the `google_maps` tool. It needs `GEMINI_API_KEY`
(`not_configured` otherwise) but not the `grounding` setting; an empty answer fails as `no_result`.

**Wolfram|Alpha** (`wolfram.query`): `GET https://api.wolframalpha.com/v2/query` with
`format=plaintext&output=json`, keeping the first subpod's text of each pod. HTTP 401/403, or an error
message mentioning the AppID, raise `WolframRejected` (`not_configured`, not retryable); a query it
cannot interpret fails as `not_understood`; anything else is `unavailable` and retryable.

Results:

| Capability | `summary` | `files` | `sources` | `data` |
| --- | --- | --- | --- | --- |
| `web.search` | Answer, then `- title: url` lines | | Result URLs | `backend`, `answer`, `results` |
| `web.read_page` | `# title`, URL, text, optional links | | The final URL | `url`, `title`, `content_type`, `links` |
| `web.image_search` | Title and source page | The image | The source page | `title`, `source_url`, `image_url` |
| `web.maps` | Answer, then place links | | Place URLs | `answer`, `results` |
| `web.wolfram` | `## pod` sections | | | `query`, `pods` |

Failures are logged with `context.log` (`search_failed`, `image_search_failed`, `maps_failed`,
`wolfram_failed`).

Safety:

* All five are `read`: they observe only, run without asking by default and may be repeated. An owner
  can still limit them per conversation with `permissions` (`use: ["*", "!web.*"]`;
  [Permissions](../../docs/core/ARCHITECTURE.md#permissions)).
* **No requests into the bot's network.** A model can be talked into reading
  `http://169.254.169.254/` or a router page; `validated_url` refuses those, including through a
  redirect. The address check and the request resolve the host name separately, so the check does not
  pin the address that is actually contacted.
* **Untrusted content.** Page text, snippets, grounded answers and image titles come from third
  parties and go to the model as tool results. The module reduces them to text and caps their size; it
  does not remove instructions written in them.
* **Keys.** `WOLFRAM_APPID` is sent as a URL parameter, so `requests` errors can contain it.
  `wolfram.query` replaces it with `***` in every message it raises (`wolfram.py:51`, `wolfram.py:54`),
  and the core also scrubs secret values from results
  ([Enforcement: secrets](../../docs/core/ARCHITECTURE.md#enforcement-secrets)). The core knows a
  value is secret when it is in an env file or named like a key; declaring the AppID as a `secret`
  setting also covers it when it is set in the real environment, since its name does not end in `KEY`.

## Data it keeps

Nothing of its own. `web.image_search` writes the image into the run's workspace
(`data/core/workspaces/<run_id>/reference-*.png` or `.jpg`), which the core deletes at startup 7 days
after the run ended.

## Tests

* [`tests/test_core_web.py`](../../tests/test_core_web.py): the grounded answer is preferred and
  SearXNG is the fallback; a failure of every backend lists each error; HTML is reduced to text, title
  and links; loopback, `localhost`, metadata, private DNS, odd ports, non-HTTP schemes, credentials and
  redirects into private networks are refused; oversized and unsupported pages are refused; `$read`
  returns page text and explains refusals.
* [`tests/test_core_web_extras.py`](../../tests/test_core_web_extras.py): image search skips unusable
  and private candidates and sends the first public image from the workspace; failures send no file;
  images are downscaled and converted to JPEG to fit; downloads are size-limited; maps uses the
  `google_maps` tool, de-duplicates places and explains failures; `$wolfram` parses legacy flags,
  formats pods, explains failures and never shows the AppID.

The tests patch `requests`, `ddgs`, DNS resolution and the Gemini client; nothing touches the network.

```
venv/bin/python -m unittest tests.test_core_web tests.test_core_web_extras
```

## Building something similar

* **A fallback chain that reports every failure**: try each backend, collect `"<name>: <error>"`, raise
  one exception with all of them (`extension.py:60-101`), and turn it into
  `ToolResult.fail(..., "unavailable", retryable=True)` at the edge (`extension.py:113-116`).
* **Optional services that degrade.** A missing key skips a backend (search) or fails clearly with
  `not_configured` (maps, Wolfram) instead of raising (`extension.py:160-161`, `extension.py:177-179`).
* **Lazy, cached clients** created on first use, so the module loads without the key or package
  (`extension.py:30-38`).
* **Blocking libraries in `asyncio.to_thread`** (`extension.py:113`, `extension.py:127`).
* **Sources** for anything taken from the web, and a file plus its source page for images
  (`extension.py:120-121`, `extension.py:151-154`).
* **Fetching user-supplied URLs**: copy `validated_url` and the manual redirect loop (`pages.py:64-140`):
  validate every hop, stream with a byte limit, check `Content-Length` and the running total.
* **Keep secrets out of messages** by replacing them before raising (`wolfram.py:45-55`), and declare
  them as `secret` settings (`extension.py:220-221`).
* **Distinct error codes** (`unavailable`, `not_configured`, `not_understood`, `no_result`,
  `invalid_arguments`) so an engine can tell the person what to fix, and `retryable=True` only for
  transient failures.

Pitfalls:

* `requests.get(url)` on a model-chosen URL follows redirects to anywhere; set `allow_redirects=False`
  and validate each `Location` yourself.
* Cap what you return: a whole page or PDF in a tool result costs the model's context.

See also: [Writing modules](../../docs/core/EXTENSIONS.md) ·
[Capabilities](../../docs/core/EXTENSIONS.md#capabilities) ·
[Permissions](../../docs/core/ARCHITECTURE.md#permissions) · [`images`](../images/README.md) ·
[`media`](../media/README.md)
