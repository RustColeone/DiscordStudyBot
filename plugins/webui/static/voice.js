"use strict";
// The students' replies read aloud, with this device's own voices (the browser's Web Speech API): free, and nothing
// leaves the device. Each student has a pitch of her own; the speed is yours. Off until you switch it on on your page
// (prefs.js `voice`, `rate`); MomoChat calls `studentVoice.speak(name, text)` for each new reply in the open chat.
(() => {
  const synth = window.speechSynthesis || null;
  const pitchOf = (name) => {
    let hash = 0;
    for (const char of String(name)) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
    return 1.05 + (hash % 6) * 0.08;  // 1.05 to 1.45: girls' voices, each her own
  };
  // The words worth saying: no code, links, Markdown or math (as Whale-chan's voice has it).
  const speakable = (text) => String(text || "")
    .replace(/```[\s\S]*?```/g, " ").replace(/\$\$[\s\S]*?\$\$|\$[^$\n]+\$/g, " ").replace(/`[^`]*`/g, " ")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/https?:\/\/\S+/g, " ")
    .replace(/^\s{0,3}(#{1,6}|[-*+]|\d+\.|>)\s+/gm, "").replace(/[*_~|]+/g, "").split(/\s+/).join(" ").trim();
  const chinese = (text) => /[㐀-鿿]/.test(text);
  const voiceFor = (text) => {
    const voices = synth ? synth.getVoices() : [];
    const wanted = chinese(text) ? /^zh/i : /^en/i;
    return voices.find((voice) => wanted.test(voice.lang) && voice.localService) || voices.find((voice) => wanted.test(voice.lang)) || null;
  };
  if (synth) synth.getVoices();  // (some browsers load their list only when first asked)
  window.studentVoice = {
    available: Boolean(synth && window.SpeechSynthesisUtterance),
    /** Say `text` in her voice; `force` even when reading aloud is off (your page's "Try it"). */
    speak(name, text, { force = false } = {}) {
      if (!this.available || (!force && !window.prefs?.get("voice"))) return false;
      const words = speakable(text).slice(0, 800);
      if (!words) return false;
      const utterance = new SpeechSynthesisUtterance(words);
      utterance.pitch = pitchOf(name);
      utterance.rate = Math.max(0.5, Math.min(2, Number(window.prefs?.get("rate")) || 1));
      const voice = voiceFor(words);
      if (voice) { utterance.voice = voice; utterance.lang = voice.lang; } else utterance.lang = chinese(words) ? "zh-CN" : "en-US";
      synth.speak(utterance);
      return true;
    },
    stop() { if (synth) synth.cancel(); },
    voices: () => (synth ? synth.getVoices().length : 0),
  };
})();
