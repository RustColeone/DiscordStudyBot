"""Translate the pages' own words into Chinese with DeepSeek (python tools/translate_ui.py [--dry-run]).

    the pages' sources (the web UI's and the Classroom's scripts and HTML) ──► their interface text: string literals
    and template literals that read as words (`${...}` becomes {0}, {1}), the HTML's text and its placeholder,
    aria-label, title and alt values, and EXTRA (words the servers send) ──► those not in the dictionary yet ──►
    DeepSeek, in batches, asked for the style of a Chinese anime gacha game's interface (二游: 体力, 好感度, 立绘…) ──►
    plugins/webui/static/i18n-zh.js, copied to the Classroom's static folder (i18n.js reads it)

The key is DEEPSEEK_API_KEY, from the environment or the .env named by --env-file (only that key is read). A
translation already in the dictionary is kept (edit it there by hand to tune it); --retranslate asks again for all.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "plugins/webui/static" / name for name in
           ("app.js", "cafe.js", "students.js", "places.js", "me.js", "index.html", "cafe.html", "students.html",
            "login.html", "me.html")] + \
          [ROOT / "extensions/chimera/web/static" / name for name in ("app.js", "index.html")]
TARGETS = [ROOT / "plugins/webui/static/i18n-zh.js", ROOT / "extensions/chimera/web/static/i18n-zh.js"]
# Words the servers send that the pages show as they are (place names, the stamina card, the core's notices).
EXTRA = ["stamina<br>today", "Cafe", "MomoChat", "Classroom", "Students", "Logbook", "Study Room", "Club Room", "The club",
         "{0} has spent her {1} for today. Let her spend {2} more today?",
         "Everyone together has spent today's {0}. Let them spend {1} more today?",
         "The session spent its budget ({0} of {1}). Let it spend {2} more?",
         "Let her", "Allow more", "Stop here", "Stamina", "here", "working", "Waiting for you", "Landed", "Discarded",
         "Didn't pass", "Answered", "Working", "Queued", "Ready for review", "Checked", "Cancelled", "Invitation",
         # single words the pages show on their own (lowercase ones are not picked up from the scripts)
         "landed", "open", "discarded", "group", "creator", "family", "friend", "acquaintance", "of", "builds", "drafts",
         "plans", "judges", "takes over", "reviews", "tests", "mood: {0}", "great", "good", "ok", "worry", "sad",
         "happy", "calm", "worried", "you", "today", "spent", "left", "stamina", "bond", "level", "her key", "their keys",
         "Stage {0} at Lv {1}", "{0} builds", "{0} drafts", "{0} plans", "{0} judges", "{0} takes over", "{0} reviews",
         "{0} tests"]
KEEP = ["MomoChat", "ChimeraArchive", "DeepSeek", "GPT", "Claude", "Gemini", "Grok", "Luna", "Sol", "mneme-llm",
        "OpenAI", "Anthropic", "Google", "xAI", "OpenRouter", "Lv", "polyglot", "Artificial Analysis"]
GLOSSARY = """stamina → 体力; today's stamina → 今日体力; bond → 好感度; bond memories → 羁绊回忆; level → 等级 (Lv stays Lv);
art stages → 立绘阶段; stage → 阶段; unlocks at → 解锁于; chibi → Q版; student → 学生; students → 学生名册 when it is the
page; student card → 学生档案; classmate → 同学; club → 社团; classroom → 教室; cafe → 咖啡厅; logbook → 日志;
session → 委托; new session → 新委托; team → 编队; seat → 席位; formation → 阵型; planner → 策划; builder → 制作;
drafter → 起草; judge → 评审; tester → 测试员; takes over → 接手; land it → 采纳; send back → 退回; discard → 放弃;
benchmark → 实测; budget → 预算; allowance → 额度; daily allowance → 每日体力上限; account → 账户; mood → 心情;
middle-schooler → 初中生; high-schooler → 高中生; university student → 大学生; your page → 个人主页; what the students
call you → 学生们对你的称呼; account → 账号 (a login: Discord, WeChat); link an account → 绑定账号; theme → 主题;
voice → 语音; creator → 创造者"""


ESCAPES = {"n": "\n", "t": "\t"}


def literals(source: str):
    """String and template literals of a script (templates with `${...}` as {0}, {1}, ...)."""
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        if source.startswith("//", index):
            index = source.find("\n", index)
            index = length if index < 0 else index
        elif source.startswith("/*", index):
            index = source.find("*/", index + 2)
            index = length if index < 0 else index + 2
        elif char in "\"'":
            end, text = index + 1, []
            while end < length and source[end] != char and source[end] != "\n":
                if source[end] == "\\" and end + 1 < length:
                    text.append(ESCAPES.get(source[end + 1], source[end + 1]))
                    end += 2
                    continue
                text.append(source[end])
                end += 1
            yield "".join(text)
            index = end + 1
        elif char == "`":
            end, text, holes = index + 1, [], 0
            while end < length and source[end] != "`":
                if source.startswith("${", end):
                    depth, end = 1, end + 2
                    start = end
                    while end < length and depth:
                        depth += {"{": 1, "}": -1}.get(source[end], 0)
                        end += 1
                    yield from literals(source[start:end - 1])  # the words inside the hole: `${t("her key")}`
                    text.append(f"{{{holes}}}")
                    holes += 1
                    continue
                if source[end] == "\\" and end + 1 < length:
                    text.append(ESCAPES.get(source[end + 1], source[end + 1]))
                    end += 2
                    continue
                text.append(source[end])
                end += 1
            yield "".join(text)
            index = end + 1
        else:
            index += 1


CODE = re.compile(r"=>|\(\)|;|#|<|>|^\.|^\[|https?:|^[a-z0-9_.:/-]+$|^[A-Z_]+$|\\n|^\$|\.(js|css|png|webp|json)\b|"
                  r"^[a-z]+(\.[a-z_]+)+|^[a-z-]+: |^\(|^[a-z-]+\(|\d+px|\}s linear")


CLASS = re.compile(r"[a-z0-9{}]+(-[a-z0-9{}]+)*")
NOT_WORDS = {"use strict", "chip wf", "avatar small", "card {0}", "chip {0}", "act {0}", "criterion {0}"}


def words(text: str) -> bool:
    """Whether a literal reads as interface words, not code."""
    bare = re.sub(r"\{\d+\}", "", text).strip()
    tokens = text.split()
    if text in NOT_WORDS or (all(CLASS.fullmatch(token) for token in tokens) and any("-" in token for token in tokens)):
        return False  # (class names: "meter meter-bond")
    if len(bare) < 2 or not re.search(r"[A-Za-z]{2}", bare) or CODE.search(text.strip()):
        return False
    return " " in text.strip() or bool(re.fullmatch(r"[A-Z][a-z]+(-[a-z]+)*[.!?…]?", bare))  # ("{0} classmates")


def collect() -> list[str]:
    found = dict.fromkeys(EXTRA)
    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".js":
            source = re.sub(r"([\"'`])[ \t]*\+[ \t]*\n\s*\1", "", source)  # "a long " +⏎ "line": one string
            for text in literals(source):
                text = " ".join(text.split())
                for part in [text, *(text.split(" · ") if " · " in text else ())]:  # (and its pieces, as i18n.js)
                    if words(part.strip()):
                        found[part.strip()] = None
        else:
            body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", "", source)
            for text in re.findall(r">([^<>]+)<", body):
                text = " ".join(text.split())
                if words(text) and "{{" not in text:
                    found[text] = None
            for text in re.findall(r'(?:placeholder|aria-label|title|alt)="([^"]+)"', body):
                if words(text) and "{{" not in text:
                    found[" ".join(text.split())] = None
    return list(found)


def ask(key: str, batch: list[str]) -> dict:
    prompt = ("Translate these English strings from a web app's interface into Simplified Chinese, in the style of a "
              "Chinese anime gacha game (二游) interface: short, lively, the usual game words. Keep every {0}, {1} "
              "placeholder exactly as it is, and keep these names as they are: " + ", ".join(KEEP) + ". Use this glossary "
              "consistently:\n" + GLOSSARY + "\nThe app: five anime girls (the students, each an AI model) who chat with "
              "you in MomoChat, hang out in the Cafe, and do coding work together in sessions in the Classroom; each "
              "has a level, a bond with you and stamina (what she may spend today). Answer with one JSON object that "
              "maps each English string, exactly as given, to its Chinese.\n\n" + json.dumps(batch, ensure_ascii=False))
    body = json.dumps({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": prompt}],
                       "response_format": {"type": "json_object"}, "max_tokens": 16000}).encode()
    request = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        answer = json.loads(response.read().decode())
    text = answer["choices"][0]["message"]["content"] or "{}"
    usage = answer.get("usage") or {}
    print(f"  {len(batch)} strings · {usage.get('prompt_tokens', 0)} in / {usage.get('completion_tokens', 0)} out", flush=True)
    try:
        return json.loads(text)
    except ValueError:  # cut off (out of tokens): the next run asks again
        return {}


def placeholders_kept(english: str, chinese: str) -> bool:
    return sorted(re.findall(r"\{\d+\}", english)) == sorted(re.findall(r"\{\d+\}", chinese))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only list the strings that would be sent")
    parser.add_argument("--retranslate", action="store_true", help="ask again for every string")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    arguments = parser.parse_args()
    current = {}
    if TARGETS[0].is_file():
        text = TARGETS[0].read_text(encoding="utf-8")
        current = json.loads(text[text.index("{"):text.rindex("}") + 1])
    strings = collect()
    todo = [item for item in strings if arguments.retranslate or item not in current]
    if arguments.dry_run:
        print("\n".join(todo))
        print(f"{len(todo)} of {len(strings)} to translate")
        return 0
    key = os.getenv("DEEPSEEK_API_KEY", "").strip().strip("\"'")
    if not key and Path(arguments.env_file).is_file():
        for line in Path(arguments.env_file).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("\"'")
    if todo and not key:
        print("DEEPSEEK_API_KEY is not set", file=sys.stderr)
        return 2
    missed = 0
    for start in range(0, len(todo), 40):
        batch = todo[start:start + 40]
        answer = ask(key, batch)
        for english in batch:
            chinese = answer.get(english)
            if isinstance(chinese, str) and chinese.strip() and placeholders_kept(english, chinese):
                current[english] = chinese.strip()
            else:
                missed += 1
        save(current, strings)  # after every batch: a failure later loses nothing
    kept = save(current, strings)
    print(f"{len(kept)} translations{f' ({missed} missed: run again)' if missed else ''} → "
          f"{', '.join(str(target.relative_to(ROOT)) for target in TARGETS)}")
    return 0


def save(current: dict, strings: list[str]) -> dict:
    kept = {english: current[english] for english in sorted(current) if english in strings}
    text = ("// The pages' words in Chinese, made by tools/translate_ui.py (DeepSeek, in the style of an anime gacha "
            "game's interface).\n// Tune a translation here; the tool keeps what is here and asks only for new words.\n"
            "window.I18N_ZH = " + json.dumps(kept, ensure_ascii=False, indent=1) + ";\n")
    for target in TARGETS:
        target.write_text(text, encoding="utf-8")
    return kept


if __name__ == "__main__":
    sys.exit(main())
