"""Optional: Piper, a local text-to-speech voice (no key, no network; https://github.com/rhasspy/piper).

Nothing imports this file unless whale_chan's config says `voice: {backend: piper}`; delete it to keep the
module light. To use it: `pip install piper-tts` (or put the `piper` program on PATH), download a voice
(for example zh_CN-huayan-medium.onnx with its .onnx.json) and set

    voice:
      backend: piper
      piper: {model: /path/to/zh_CN-huayan-medium.onnx}   # command: piper, speaker: 0, length_scale: 1.0

Piper writes WAV; with ffmpeg on PATH it becomes Ogg Opus, so Discord shows a voice message.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping


class PiperVoice:
    name = "piper"

    def __init__(self, config: Mapping[str, Any]):
        self.command = str(config.get("command", "piper"))
        self.model = str(config.get("model", ""))
        self.speaker = config.get("speaker")
        self.length_scale = config.get("length_scale")

    def ready(self) -> str:
        if not shutil.which(self.command):
            return f"the {self.command} program is not installed"
        if not self.model or not Path(self.model).is_file():
            return "no Piper voice model file is configured"
        return ""

    def synthesize(self, text: str, path: Path) -> Path:
        wav = path.with_suffix(".wav")
        command = [self.command, "--model", self.model, "--output_file", str(wav)]
        if self.speaker is not None:
            command += ["--speaker", str(self.speaker)]
        if self.length_scale is not None:
            command += ["--length_scale", str(self.length_scale)]
        done = subprocess.run(command, input=text.encode(), capture_output=True, timeout=120)
        if done.returncode != 0 or not wav.is_file():
            raise RuntimeError(done.stderr.decode("utf-8", "replace")[-300:] or "piper produced no audio")
        if shutil.which("ffmpeg"):
            converted = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus",
                                        "-b:a", "32k", str(path)], capture_output=True, timeout=120)
            if converted.returncode == 0 and path.is_file():
                wav.unlink(missing_ok=True)
                return path
        path.unlink(missing_ok=True)
        return wav
