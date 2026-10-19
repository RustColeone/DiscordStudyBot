# `images`: image generation with Gemini or OpenAI

`images` draws pictures from a description, or edits and restyles images people attached, with
Google Gemini or OpenAI, and sends the result to the conversation. It is one capability in one file,
which makes it the smallest example of calling an outside model API with a key from `.env`, writing
output into the run's workspace and returning it as files.

## What it adds

| Capability | Tier | Access | Command | Needs | Notes |
| --- | --- | --- | --- | --- | --- |
| `images.generate` | `prepare` | member | `$draw <prompt>` | `files` | `timeout_s=300`; parameters `prompt` (required), `provider` (`gemini` or `openai`), `use_attached_images` |

| Setting | Kind | Default | Env |
| --- | --- | --- | --- |
| `default_provider` | `text` | `gemini` | |

It sets no `private`, `never_auto` or `confirm`, so by default it runs without asking. There are no
other contributions.

## Setup

`config.example.yml` (copy it to `config.yml` next to it; `extension_config.images` in `core.yml`
overrides it):

```yaml
default_provider: gemini        # or openai
gemini_model: gemini-2.5-flash-image
openai_model: gpt-image-2.5-sunburst
size: 1024x1024
quality: auto
output_format: png
```

`default_provider` draws when the call names no provider; `gemini_model` and `openai_model` are the
models; `size`, `quality` and `output_format` go to OpenAI's image API only (Gemini uses its defaults).

`.env`: `GEMINI_API_KEY` and/or `OPENAI_API_KEY`. A value of `disabled`, `none` or `null` counts as
unset. Packages: `google-genai`, `openai` (both in [`requirements.txt`](../../requirements.txt)).
To turn the module off, put `extensions/images` in `core.yml`'s `modules: {exclude: [...]}`.

## Using it

```
$draw a watercolor fox reading a book
```

Asking the assistant works too ("draw this in anime style" with a photo attached). The model sets
`use_attached_images` to edit or restyle the images attached to the run's messages, and
`provider: openai` only when the person asks for GPT, ChatGPT, OpenAI or DALL-E. The reply is
"Generated N image(s) with <provider> (<model>)." plus the model's own note (OpenAI's revised prompt or
Gemini's text, up to 300 characters), and the images are sent as files. Reference images must be
`image/*` files of at most 20 MB.

## How it works

[`extension.py`](extension.py) holds everything: `ImageGenerator` (both providers), the handler, the
capability and the setting; [`config.example.yml`](config.example.yml) documents the settings.

1. The handler (`extension.py:117-138`) picks the provider and, with `use_attached_images`, the image
   attachments from `tool.files`; with none it fails as `no_reference_image`.
2. `await tool.progress("Drawing with <provider>...")`, then `generator.generate` runs in
   `asyncio.to_thread`, since both SDKs are blocking.
3. OpenAI (`_openai`): `images.edit` with the references as named byte streams, or `images.generate`
   without them, then each `b64_json` item is decoded. Gemini (`_gemini`): `models.generate_content`
   with the prompt and the reference bytes as parts and `response_modalities=["IMAGE", "TEXT"]`;
   inline image parts become files and text parts the note.
4. `_write` saves each image with `tempfile.mkstemp` in `tool.workspace` (`openai-*.png`,
   `gemini-*.png`, …). The result carries them as `FileRef`s with a MIME type from the suffix, and
   `data={"provider", "model"}`; there are no `sources`.

Clients are created on first use per provider and cached (`_client`). A missing key raises
`LookupError`, which becomes `not_configured`; any other exception becomes `generation_failed` with
`retryable=True` and the exception's type and message, cut to 800 characters. There is no fallback
between providers: if the one asked for (or the default) is not configured or fails, the call fails.

Safety: it creates local files only, hence `prepare`; the services receive the prompt and any
reference images. The keys are named `*_API_KEY`, so the core treats their values as secrets
and scrubs them from results ([Enforcement: secrets](../../docs/core/ARCHITECTURE.md#enforcement-secrets)).

## Data it keeps

Nothing of its own. Images live in the run's workspace (`data/core/workspaces/<run_id>/`), deleted at
startup 7 days after the run ended.

## Tests

[`tests/test_core_ported.py`](../../tests/test_core_ported.py) (`ImageTests`) replaces
`ImageGenerator.generate`, so no API is called: `$draw` delivers the generated file with the provider
in the reply, a missing key is reported as not configured, `use_attached_images` without an image
fails, and attached images reach the generator as references.

```
venv/bin/python -m unittest tests.test_core_ported
```

## Building something similar

* **Keys from `.env`, read when first needed**, with placeholder values treated as unset (`extension.py:21-23`),
  and SDK clients created lazily so the module loads without them (`extension.py:39-52`).
* **Blocking SDK calls in `asyncio.to_thread`** (`extension.py:127-128`) after a progress line; output
  into the workspace with unique names (`extension.py:105-111`), returned as `FileRef`s (`extension.py:134-138`).
* **Map failures to codes** at the edge (`extension.py:129-133`): one for setup problems, one for the
  service failing.
* **A test seam**: keep the outside call in one method (`ImageGenerator.generate`) so tests can patch it.

Pitfalls: check attachments yourself (type, existence, size) before sending them to a service, since
`needs` does not cover incoming files; and keep error text short, because it reaches the person.

See also: [Writing modules](../../docs/core/EXTENSIONS.md) ·
[Capabilities](../../docs/core/EXTENSIONS.md#capabilities) ·
[Model providers](../../docs/core/EXTENSIONS.md#model-providers) · [`web`](../web/README.md) ·
[`media`](../media/README.md)
