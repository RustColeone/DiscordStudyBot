# `media`: video downloads and clips fitted to the upload limit

`media` downloads videos from YouTube, Bilibili and the other sites yt-dlp supports, and delivers
them as files that fit the conversation's upload limit. `$video` sends a whole video; `$clip` first
shows a preview of one or more clips with size estimates, which can be trimmed, compressed or re-tuned
before anything is rendered. Clips can also be GIFs or audio only. Study it for long-running work with
progress messages, files and sources in a `ToolResult`, asking the person instead of guessing, state
that survives a restart, and blocking tools (yt-dlp, ffmpeg) run from async code.

## What it adds

| Capability | Tier | Access | Command | Needs | Notes |
| --- | --- | --- | --- | --- | --- |
| `media.download_video` | `prepare` | member | `$video <url>` (message action "Download video") | `files` | `timeout_s=1800` |
| `media.clip_video` | `prepare` | member | | `files` | Creates a pending preview; renders nothing. `timeout_s=300` |
| `media.adjust_pending_clip` | `prepare` | member | | | `trim`, `fit_upload`, `keep_full_quality`, `set_quality` |
| `media.manage_pending_clips` | `prepare` | member | | `files` | `confirm` (render and deliver) or `cancel`. `timeout_s=1800` |
| `media.list_pending_clips` | `read` | member | | | |
| `media.clip_command` | `prepare` | member | `$clip <flags>` | `files` | `internal` (not offered to models). `timeout_s=1800` |

None of them sets `private`, `never_auto` or `confirm`, so under default permissions all of them run
without asking ([tiers](../../docs/core/ARCHITECTURE.md#tiers-and-attributes)).

| Setting | Kind | Default | Env |
| --- | --- | --- | --- |
| `keep_dir` | `path` | `clips` | |
| `cookies_file` | `path` | none | (`MEDIA_COOKIES_FILE`, the older way) |

No other contributions (no interfaces, engines, personas or providers).

## Setup

`config.example.yml` (copy it to `config.yml` next to it; `extension_config.media` in `core.yml`
overrides it):

```yaml
keep_dir: clips                 # --keep-file copies (relative to data/core/extensions/media)
# cookies_file: cookies.txt       # yt-dlp cookies (Netscape format) for sites that need a login; relative to
                                  # data/core/extensions/media, or absolute (MEDIA_COOKIES_FILE in .env still works)
```

* `keep_dir`: where clips are copied when someone asks to keep the file on the bot host. A relative
  path is resolved against the module's data folder (`context.data_dir`, by default
  `data/core/extensions/media`); `~` is expanded.

* `cookies_file`: a cookies file passed to yt-dlp as `cookiefile`, for sites that need a login. A
  relative path is resolved against the module's data folder. Used only if the file exists. Without it,
  `MEDIA_COOKIES_FILE` from the environment is used (the older way; `.env` is meant for secrets). The
  file itself holds your login session, so keep it readable by the bot's user only.

Packages and tools:

* `yt-dlp` (in [`requirements.txt`](../../requirements.txt)), imported only when it is needed.
* `ffmpeg` on the `PATH`. The module runs it as `ffmpeg`, and yt-dlp needs it to merge separate video
  and audio streams.

To turn the module off, exclude it in `core.yml`:

```yaml
modules:
  exclude: [extensions/media]
```

## Using it

**`$video <url>`** downloads the whole video and fits it to the upload limit. Asking the assistant
instead ("send me 1:05 to 2:30 of this, under 5 MB") lets the model pass the other arguments of
`media.download_video`: `start`, `end`, `max_size_mb` (only smaller than the limit), `resolution`,
`fps`, `bitrate`, `audio_bitrate`. If the length cannot fit at usable quality, the bot asks whether a
shorter range is fine; nothing is downloaded.

**`$clip`** works in two steps: preview, then confirm.

```
$clip -u https://youtu.be/… -s 1:05 -e 1:20          preview one clip
$clip -u <url1> -s 0:10 -e 0:30 -u <url2> -e 1:00    several clips; -s/-e pair up with -u in order
$clip                                                show the pending clips
$clip --clip 1 --fit                                 compress clip 1 to fit the upload limit
$clip --clip 2 -r 480p --fps 24 -b 800k              set quality by hand
$clip --clip 1 -s 1:10 -e 1:18                       trim clip 1
$clip --clip 1 -f gif                                change the format
$clip --keep-file                                    keep the rendered files on the host (all clips)
$clip --confirm --skip 2                             render and send every clip except 2
$clip --cancel                                       discard the pending clips
$clip -u <url> -e 0:30 -f mp3 --force                render at once, without a preview
```

Flags: `-u/--url`, `-s/--start`, `-e/--end`, `-r/--resolution` (like `720p`), `-b/--bitrate` (like
`1500k`), `-f/--format`, `--fps` (1 to 60), `--clip <N>`, `--skip <N>`, `--fit`, `--keep-file`,
`--delete-file`, `--force`, `--confirm`, `--cancel`. Unknown flags and bad values are reported with
the usage text and nothing is downloaded.

* Times: `65`, `1:05`, `1:05.5` or `1:02:03`. An end past the video's length is clamped to it.
* Formats: `mp4` (default), `webm`, `mov`, `mkv`, `gif`, `mp3`, `m4a`, `wav`, `aac`, `ogg`, `flac`.
* The preview lists each clip's source, range, selected settings, estimated size against the limit,
  and quality options A, B, C… with a ✅ or ❌ for whether each fits:

| Output | Options (resolution, bitrate, fps) |
| --- | --- |
| Video | 1080p 2500k 30, 720p 1500k 30, 480p 1000k 30, 360p 600k 30 (audio 128k) |
| GIF | 720p 1000k 15, 480p 600k 15, 360p 400k 10 |
| Audio | 320k, 192k, 128k (only those that fit, or the smallest) |

GIFs are always rendered 360 pixels high at the selected fps, so for them the resolution and bitrate
only change the estimate. `webm`, `mov` and `mkv` use ffmpeg's default codecs for the container at the
selected video bitrate. A new clip starts from the first option (for audio, the best one that fits)
unless settings were given. Rendered files are named `clip_1.mp4`, `clip_2.gif` and so on.

Through the assistant, the model calls `media.clip_video`. When the clip is above the limit, the result
asks the person's goal: trim it, compress it (`fit_upload`), or keep the current quality and only keep
the file on the host (`keep_full_quality`). It then adjusts the pending clip and confirms with
`media.manage_pending_clips`.

Limits:

* The budget is the conversation's `display.max_upload_mb`, reported by its interface (the Discord
  interface reports the server's file size limit, 10 MB by default). A rendered file above it is
  deleted, or, with `--keep-file`, copied to `keep_dir` and not attached.
* Fitting gives the video at most 2500 kbps and 720p at 24 fps, and gives up below 96 kbps of video
  after trying audio at 128k, 96k and 64k.
* yt-dlp: no playlists, a 15 s socket timeout, one retry.
* Timeouts: 30 minutes for `$video`, `$clip` and confirming; 5 minutes for a preview.

## How it works

| File | Role |
| --- | --- |
| [`extension.py`](extension.py) | Declares the capabilities, the two commands and the `keep_dir` and `cookies_file` settings (passing the cookies file to `video.py`); `download_video` |
| [`video.py`](video.py) | yt-dlp inspection and download, budget fitting, stream-copy selection, ffmpeg encoding |
| [`clip.py`](clip.py) | Pending clips: estimates, preview text, trim/fit/quality changes, the store, rendering, the `$clip` parser |
| [`config.example.yml`](config.example.yml) | Documented settings |

**`media.download_video`** (`extension.py:15-72`):

1. `context.progress("Inspecting the video source...")`, then `video.video_info` asks yt-dlp for the
   title, site and duration without downloading (`extract_info(download=False)` in
   `asyncio.to_thread`).
2. The budget is the smaller of `context.display.max_upload_mb` and `max_size_mb`. `video.fit_budget`
   computes the video bitrate that leaves 10% for muxing:
   `min(2500, budget_mb × 0.90 × 8 × 1024 / seconds − audio_kbps)`, and picks 360p under 400 kbps,
   480p under 800 kbps, else 720p. If no audio bitrate gives at least 96 kbps of video, it returns
   `ToolResult.ask(..., error_code="compression_budget")` before downloading anything.
3. `video.render` downloads with yt-dlp. For a full video with no explicit quality it first looks for a
   stream copy: `select_copy_formats` picks the largest H.264 video plus AAC audio pair (mp4/m4a, not
   HLS, at most 1080p) whose reported size fits 95% of the budget, and ffmpeg copies it without
   re-encoding (`-c copy`). If the copy does not fit, it re-encodes. Otherwise ffmpeg encodes the range
   (`libx264 -preset veryfast`, AAC, scaled, `+faststart` for mp4). The downloaded source is deleted in
   a `finally`.
4. The output is measured again; above the budget it is deleted and the call fails with
   `output_budget_exceeded`.
5. Success returns the file and its origin:
   `ToolResult.ok(summary, files=(FileRef(path, name, "video/mp4", size),), sources=(url,), data={method,
   duration_s, size_mb, budget_mb})`. The core sends the file; the summary says whether it was copied
   or re-encoded.

**Pending clips** (`clip.py`): `create` inspects each URL and builds a clip dict with the best option's
settings. The list is saved with `context.storage.set("pending:<address>", …)`, the module's private
store, so a preview belongs to one conversation and survives a restart. `pick`, `trim`, `fit` and
`set_quality` change one clip and re-estimate its size; changing between video, GIF and audio starts
from that kind's best option. `confirm` renders the clips (`process`), sends those within the limit,
copies kept ones to `keep_dir` as `<title>_<run_id>_<N>.<ext>`, and clears the pending list only if it
is unchanged since it was loaded, because another run may have made a new preview meanwhile.

`preview_result` answers people and models differently: `$clip` gets the preview with a command list
(`commands=True`); a model gets the preview plus either "Nothing is rendered yet. Confirm with …" or a
`needs_input` result (`clip_goal_needed`) when a clip is above the limit.

Error codes: `invalid_arguments`, `invalid_range`, `source_unavailable` (yt-dlp could not inspect or
download), `compression_budget`, `render_failed` (ffmpeg failed), `output_budget_exceeded`,
`no_pending_clip`, `clip_goal_needed`.

Design decisions:

* **Preview before rendering.** Downloading and encoding are slow and costly; the estimate is cheap.
  The person sees the size against the limit and chooses, instead of the bot picking a quality.
* **Ask, do not guess.** When a request cannot fit, the capability returns `ToolResult.ask` with the
  options, and the engine relays the question.
* **The limit comes from the conversation.** `context.display.max_upload_mb` is reported by the
  interface, so the same code fits a small chat limit, a larger server limit and a web page.
* **Blocking work off the event loop.** yt-dlp runs in `asyncio.to_thread`; ffmpeg runs through
  `asyncio.create_subprocess_exec`. While a run waits on the capability it does not hold the
  conversation's lane, so people can keep talking ([Concepts](../../docs/core/EXTENSIONS.md#concepts)).
* **Two front doors.** Models get small structured tools (`clip_video`, `adjust_pending_clip`,
  `manage_pending_clips`); people get the `$clip` flags through the `internal` capability
  `media.clip_command`. Both use the same functions in `clip.py`.

Safety:

* Everything is `prepare` (`list_pending_clips` is `read`): it creates local files and changes nothing
  shared, so by default it runs without approval, and the core never repeats it within a run.
  An owner can make it ask with `permissions` ([Permissions](../../docs/core/ARCHITECTURE.md#permissions)).
* `needs={"files"}` keeps the file-producing capabilities away from conversations that cannot show
  files; they are refused before anything is downloaded.
* URLs go to yt-dlp as given; unlike `web.read_page`, the module does not restrict which hosts are
  contacted.
* `--keep-file` writes to the host's disk and nothing removes those copies.
* The cookies file holds login sessions; keep it out of the repository and readable only by the bot.

## Data it keeps

| What | Where | Kept |
| --- | --- | --- |
| Rendered files | The run's workspace (`data/core/workspaces/<run_id>/`) | Deleted at startup 7 days after the run ended ([Retention](../../docs/core/EXTENSIONS.md#retention)) |
| Pending clips | The core's store for this module (`extension:media`), key `pending:<address>` | Until confirmed or cancelled; there is no expiry |
| Kept files (`--keep-file`, `keep_full_quality`) | `keep_dir` (default `data/core/extensions/media/clips/`) | Indefinitely |

## Tests

* [`tests/test_core_clip.py`](../../tests/test_core_clip.py): the preview, adjusting and confirming with
  `--skip`; pending clips surviving a restart and staying in their conversation; the model flow that
  asks for a goal, trims, sets quality and confirms; `fit_upload` against the limit; audio clips and
  kept files; oversized results deleted or kept; argument and source errors reported without
  downloading; refusal in a conversation without files.
* [`tests/test_core_ported.py`](../../tests/test_core_ported.py) (`MediaTests`): `$video` stream-copies a
  fitting source, a range is re-encoded, an unfittable length asks instead of downloading, and
  conversations without files cannot use it.

Both replace `yt_dlp` in `sys.modules` with a fake that copies a two-second fixture generated by ffmpeg,
so no network is used. They are skipped when `ffmpeg` is not installed.

```
venv/bin/python -m unittest tests.test_core_clip tests.test_core_ported
```

## Building something similar

* **Progress for long work.** Call `await context.progress(text)` at each stage (`extension.py:17`,
  `video.py:150`, `video.py:169-179`, `clip.py:307`); the interface shows it (Discord edits one message
  in place).
* **Files and sources in the result.** Write output into `context.workspace` and return `FileRef`s plus
  the source URLs (`extension.py:69-72`, `clip.py:347-356`). Return files only on success; a failed
  result's files are not sent.
* **Fit the conversation, not a constant.** Read `context.display.max_upload_mb` (`extension.py:28`,
  `clip.py:195`) and check the real size after rendering (`extension.py:57-61`).
* **Ask with `ToolResult.ask`** when the request needs a decision, and include the options
  (`extension.py:41-44`, `clip.py:204-213`).
* **Per-conversation state** in `context.storage` keyed by `context.address` (`clip.py:109-122`), and
  re-check it before clearing (`clip.py:371-373`).
* **Blocking libraries** in `asyncio.to_thread` (`video.py:131-136`, `video.py:165`), subprocesses with
  `asyncio.create_subprocess_exec` (`video.py:211-215`); clean temporary files in `finally`
  (`video.py:185-187`).
* **Map exceptions to error codes**: a small exception type per cause (`VideoSourceError`,
  `ClipError(message, code)`) and `ToolResult.fail(message, code)` at the edge (`extension.py:22-25`,
  `extension.py:91-92`). Expected failures should not raise out of the handler.
* **A rich command**: a `Command` with `Option`s, flags and a `template` renders slash-command values
  back into the `$clip` text your parser reads (`extension.py:190-204`;
  [Declaring commands](../../docs/core/EXTENSIONS.md#declaring-commands)).

Pitfalls:

* Do not import heavy optional packages at module level; `video.py` imports `yt_dlp` inside the
  functions, so the module loads (and tests can patch it) without it.
* Choose `timeout_s` for the slowest real case; the default is 600 s.
* Declare `needs={"files"}` on anything whose result is a file.
* An estimate is not a size: measure the output before attaching it.

See also: [Writing modules](../../docs/core/EXTENSIONS.md) ·
[Capabilities](../../docs/core/EXTENSIONS.md#capabilities) ·
[Permissions](../../docs/core/ARCHITECTURE.md#permissions) · [`web`](../web/README.md) ·
[`images`](../images/README.md)
