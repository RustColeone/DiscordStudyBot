"""One step of a speaker, in either form her model answers in.

Where her model can call tools as functions (OpenAI-compatible providers, `tool_calls` on by default), a step is her
words and her calls: the capabilities, and the engine's own tools (`ask` waits for the person's answer, `pass` stays
silent when she was only invited to join in, `ask_classmate` hands part of the work to another persona in the chat).
Otherwise, and for a closing step or a speak-up, it is one JSON object (TURN_SCHEMA), where a hand-off is an action
of `ask_classmate` and staying silent is the status `pass`. The transcript keeps both forms, so a step can fall back
from one to the other.
"""

from __future__ import annotations

import json

ASK, PASS, HAND_OFF = "ask", "pass", "ask_classmate"  # the engine's own tools (no dots, so no capability has them)
MAX_CALLS = 5  # actions per step, as TURN_SCHEMA allows
NO_ARGUMENTS = {"type": "object", "properties": {}}
ASK_PARAMETERS = {"type": "object", "properties": {"question": {"type": "string", "minLength": 1}},
                  "required": ["question"], "additionalProperties": False}
HAND_OFF_PARAMETERS = {"type": "object", "properties": {
    "classmate": {"type": "string", "description": "Her name"},
    "request": {"type": "string", "minLength": 1, "description": "What she should do, with everything she needs"}},
    "required": ["classmate", "request"], "additionalProperties": False}

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "status": {"type": "string", "enum": ["continue", "complete", "needs_input", "blocked", "pass"]},
        "actions": {"type": "array", "maxItems": MAX_CALLS, "items": {
            "type": "object",
            "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["tool", "arguments"], "additionalProperties": False}},
    },
    "required": ["message", "status", "actions"],
    "additionalProperties": False,
}

# ---- what she is told about the form of her answer --------------------------------------------------------------
# `brief`: a speaker in a conversation (the assistant's and the room's way). `full`: a personal AI chat client (the
# chat engine's way, Whale-chan's bot): complete answers in Markdown, only the answer posted.
CONTRACT = {"brief": """\
Respond to every turn with one JSON object: {{"message": string, "status": "continue"|"complete"|"needs_input"|"blocked"{pass_status}, "actions": [{{"tool": name, "arguments": {{...}}}}]}}.
- message: what you say to the person now, in their language and your own voice. Keep it brief; it may be empty while acting.
- With actions, the message only says what you are about to do. Never state an action's outcome (saved, sent, done) before its [tool] result appears.
- continue: you run the listed actions now and see their results next turn. continue requires at least one action.
- complete: the request is handled. needs_input: you asked one question and wait for the answer. blocked: you cannot finish; say why briefly. These take no actions.
- Tool results appear as notes starting with [tool]. Never claim a result you have not seen.
- Files from successful tools are delivered to the person automatically; do not deliver them again.
- An action may wait for the owner's approval. If so, tell the person briefly that it is waiting.
{display}
Available tools:
{tools}""", "full": """\
You are a full assistant, like a personal AI chat client: give complete, accurate, well-structured answers \
(Markdown headings, lists, tables and fenced code blocks; math in LaTeX with $…$ inline and $$…$$ on its \
own lines). Match the length to the question: short for chat, thorough for real questions. Answer in the \
person's language.
Each step returns one JSON object: {{"message": string, "status": "continue"|"complete"|"needs_input"|"blocked"{pass_status}, "actions": [{{"tool": name, "arguments": {{...}}}}]}}.
- complete: "message" is your answer.
- continue: run the listed actions (at least one) when tools would help (current facts, web pages, files, images, calculations); "message" is a short note on what you are doing, shown as progress. You see the results next step. Never claim a result you have not seen; results appear as notes starting with [tool].
- needs_input: ask one question when you truly cannot proceed. blocked: say plainly what you could not do and why.
- Files from successful tools are delivered automatically. Writes may wait for the owner's approval.
{display}
Available tools:
{tools}""", "chat": """\
You are chatting with someone you know, in a chat app: answer the way you would text them, short and casual, in \
their language and your own voice; only when they ask for something long, be as thorough as it takes.
Each step returns one JSON object: {{"message": string, "status": "continue"|"complete"|"needs_input"|"blocked"{pass_status}, "actions": [{{"tool": name, "arguments": {{...}}}}]}}.
- complete: "message" is your answer.
- continue: run the listed actions (at least one) when tools would help; "message" is a short note on what you are doing, shown as progress. Never claim a result you have not seen.
- needs_input: ask one question and wait. blocked: say briefly what you could not do and why.
- Files from successful tools are delivered automatically. Writes may wait for the owner's approval.
{display}
Available tools:
{tools}"""}

NATIVE_CONTRACT = {"brief": """\
Call the tools as functions when they help; you see each result before your next step. When you are done, answer \
in words, with no calls.
- What you say is for the person now, in their language and your own voice. Keep it brief; while you call tools it \
only says what you are about to do. Never state an action's outcome (saved, sent, done) before its result appears, \
and never claim a result you have not seen.
- To ask the person one question and wait for their answer, call ask, alone.
- If you cannot finish, say why briefly.
- Files from successful tools are delivered to the person automatically; do not deliver them again.
- A call may wait for the owner's approval. If so, tell the person briefly that it is waiting.
{display}""", "full": """\
You are a full assistant, like a personal AI chat client: give complete, accurate, well-structured answers \
(Markdown headings, lists, tables and fenced code blocks; math in LaTeX with $…$ inline and $$…$$ on its \
own lines). Match the length to the question: short for chat, thorough for real questions. Answer in the \
person's language.
Call the tools as functions when they would help (current facts, web pages, files, images, calculations); you see \
each result before your next step. When you are done, your words, with no calls, are your answer.
- While you call tools, your words are a short note on what you are doing, shown as progress. Never claim a result \
you have not seen.
- To ask one question when you truly cannot proceed, call ask, alone. If you cannot do something, say plainly what \
and why.
- Files from successful tools are delivered automatically. Writes may wait for the owner's approval.
{display}""", "chat": """\
You are chatting with someone you know, in a chat app: answer the way you would text them, short and casual, in \
their language and your own voice; only when they ask for something long, be as thorough as it takes.
Call the tools as functions when they help; you see each result before your next step. When you are done, your \
words, with no calls, are your answer.
- While you call tools, your words are a short note on what you are doing, shown as progress. Never claim a result \
you have not seen.
- To ask one question and wait for the answer, call ask, alone. If you cannot do something, say why briefly.
- Files from successful tools are delivered automatically. Writes may wait for the owner's approval.
{display}"""}

# A chat with several personas (a group chat): who they are, and how they work together.
GROUP = """\
This is a group chat with {roster}. Speak only for yourself, in your own voice: never answer in another's place, \
say what they think, or claim they agree before they have said so. Do not repeat what someone already said in it. \
For ordinary chat, one useful thought at a time, usually one to three sentences. Do not invent off-screen \
activities, past conversations or experiences.
{hand_off}"""
HAND_OFF_RULE = {
    True: "To have a classmate do part of the work (search for something, draw an image, write a prompt, check "
          "your answer), call ask_classmate with her name and the request, with everything she needs: she answers "
          "in the chat after you, and you see her answer and can finish with it then. Hand off only what she is "
          "better placed to do; never ask her to repeat your work.",
    False: "To have a classmate do part of the work (search for something, draw an image, write a prompt), add an "
           "action of the tool ask_classmate, {\"classmate\": her name, \"request\": what she should do, with "
           "everything she needs}: she answers in the chat after you, and you see her answer and can finish with it "
           "then.",
}
PASS_RULE = {True: "- You may stay silent this time: call pass, alone, when you have nothing worth adding.",
             False: "- You may stay silent this time: status pass, with an empty message and no actions, when you "
                    "have nothing worth adding."}

# Why a speaker who is not the one answering speaks (her turn's instruction, in the turn's context).
CHIME = ("The person wrote to the group without naming anyone, and {lead} answered them. Join in only if you have "
         "something worth adding (a different view, a fact, a reaction) and keep it short; otherwise pass. Do not "
         "repeat what {lead} said.")
MENTION = ("The person spoke to {lead} and mentioned you in passing; nobody asked you. Join in only if you genuinely "
           "want to add to {lead}'s answer (something you know first-hand, a correction, a reaction to being "
           "mentioned), briefly and in your own voice, as your own choice to speak up. Never remark on who was asked "
           "or answer in {lead}'s place. Otherwise pass.")
ASKED = ("{asker} asked you, in this chat: {request}\nDo it, with your tools if it needs them, and answer in the chat "
         "yourself: the person reads it.")
FOLLOW_UP = ("You asked {classmate} to do part of this; her answer is in the chat now. Finish what the person asked "
             "with it, briefly, without repeating her; or pass if nothing is left to say.")

SPEAK_UP = """\
(Nobody is talking right now. You decided on your own to message {name}: {motive}. Write the message you send: \
short and natural, the way you message someone on impulse, in your own voice and their language. No tools.)"""
CLOSING = ("You must stop now and cannot run more actions. Reply with status blocked (or complete if it is done) and "
           "no actions: briefly tell the person what is done and what is unfinished or unverified.")

# What changes from turn to turn (the time, her memory of the person, other requests in progress, this turn's
# instruction) comes in one message after the history rather than in the system prompt, so the system prompt and the
# history stay one cached prefix.
TURN_CONTEXT = "[This turn's context, from the system, not from the person]"


def answer(action: dict, content: str, native_only: bool = False) -> dict:
    """A transcript note answering an action: a function call's result when the action came from one."""
    note = {"role": "user", "content": content}
    if action.get("tool_call_id"):
        note["tool_call_id"] = action["tool_call_id"]
        if native_only:
            note["native_only"] = True  # (a JSON step sees the failure itself)
    return note


def waiting(action: dict, content: str) -> dict:
    """The note that an action waits for the owner (shown in the JSON form; a function call gets its result)."""
    return {"role": "user", "content": content, **({"waiting": True} if action.get("tool_call_id") else {})}


def transcript(items, native: bool) -> list[dict]:
    """The turn's transcript as request messages: as function calls and their results (`native`), or as JSON
    steps and [tool] notes. Notes only a function call needs are left out of the JSON form."""
    messages = []
    for item in items:
        if native and item["role"] == "assistant" and item.get("native"):
            step = item["native"]
            messages.append({"role": "assistant", "content": step.get("text") or "",
                             "tool_calls": list(step.get("tool_calls") or ()),
                             **({"replay": step["replay"]} if step.get("replay") else {})})
        elif native and item.get("waiting"):
            continue  # (its call's result says what came of it, right after the call)
        elif native and item.get("tool_call_id"):
            messages.append({"role": "tool", "tool_call_id": item["tool_call_id"], "content": item["content"]})
        elif native and item["role"] == "assistant":
            messages.append({"role": "assistant", "content": said(item["content"])})
        elif not item.get("native_only"):
            messages.append({"role": item["role"], "content": item["content"]})
    return messages


def said(content: str) -> str:
    """A JSON step as plain words (in a request with function calls, raw JSON would invite more of it)."""
    try:
        turn = json.loads(content)
    except ValueError:
        return content
    if not isinstance(turn, dict) or "message" not in turn:
        return content
    called = ", ".join(str(action.get("tool")) for action in turn.get("actions") or () if isinstance(action, dict))
    return "\n".join(part for part in (str(turn.get("message") or "").strip(), f"[called {called}]" if called else "")
                     if part) or "(working)"


def from_calls(result, optional: bool = False) -> tuple[dict, str | None]:
    """A function-calling answer as her step (with what the transcript needs to replay it), or the problem to send
    back. `optional`: she was only invited to join in, so she may pass."""
    actions, questions, handoffs, passed = [], [], [], False
    for index, call in enumerate(result.tool_calls):
        if call.name == ASK:
            questions.append(str(call.arguments.get("question") or "").strip())
        elif call.name == PASS:
            passed = True
        elif call.name == HAND_OFF:
            handoffs.append({"classmate": str(call.arguments.get("classmate") or "").strip(),
                             "request": str(call.arguments.get("request") or "").strip()})
        else:
            actions.append({"tool": call.name, "arguments": dict(call.arguments), "call_index": index})
    text = (result.text or "").strip()
    if passed and (len(result.tool_calls) > 1 or text):
        return {}, "pass takes no words and no other calls; call it alone."
    if passed and not optional:
        return {}, "pass is only for a turn you were invited to join; answer the person."
    if questions and (actions or handoffs or len(questions) > 1):
        return {}, "ask takes no other calls; call it alone, once."
    if len(actions) + len(handoffs) > MAX_CALLS:
        return {}, f"at most {MAX_CALLS} calls per step."
    if questions and not questions[0]:
        return {}, "ask needs a question."
    if any(not item["classmate"] or not item["request"] for item in handoffs):
        return {}, "ask_classmate needs her name and the request."
    if not actions and not questions and not text and not handoffs and not passed:
        return {}, "your answer needs words."
    message = text
    if questions and "?" not in text and "？" not in text:  # her words may already ask it
        message = "\n\n".join(part for part in (text, questions[0]) if part)
    # a hand-off and no words: a step of its own (the call is answered; her words come next step)
    status = ("pass" if passed else "continue" if actions or (handoffs and not text) else
              "needs_input" if questions else "complete")
    native = {"text": result.text or "", "tool_calls": [{"id": call.id, "name": call.name,
                                                          "arguments": dict(call.arguments)}
                                                         for call in result.tool_calls]}
    if result.replay:
        native["replay"] = dict(result.replay)
    return {"message": message, "status": status, "actions": actions, "handoffs": handoffs, "_native": native}, None


def from_json(turn: dict, names: set[str], optional: bool, final: bool) -> tuple[dict, str | None]:
    """A JSON step as her step, or the problem to send back: hand-offs are actions of `ask_classmate`."""
    handoffs = [{"classmate": str((action.get("arguments") or {}).get("classmate") or "").strip(),
                 "request": str((action.get("arguments") or {}).get("request") or "").strip()}
                for action in turn["actions"] if action["tool"] == HAND_OFF]
    actions = [action for action in turn["actions"] if action["tool"] != HAND_OFF]
    turn = {**turn, "actions": actions, "handoffs": handoffs}
    unknown = [action["tool"] for action in actions if action["tool"] not in names]
    if unknown:
        return {}, f"unknown tools {unknown}; use only the listed tools."
    if optional and turn["status"] not in ("complete", "pass"):
        return {}, "when joining in, either answer briefly with status complete or pass."
    if turn["status"] == "pass":
        if not optional:
            return {}, "pass is only for a turn you were invited to join; answer the person."
        if actions or handoffs or turn["message"].strip():
            return {}, "pass takes an empty message and no actions."
        return turn, None
    if final and (turn["status"] == "continue" or actions or handoffs):
        return {}, "you cannot run more actions; answer with status complete or blocked."
    if any(not item["classmate"] or not item["request"] for item in handoffs):
        return {}, "ask_classmate needs her name and the request."
    if turn["status"] == "continue" and not (actions or handoffs):
        return {}, "status continue requires at least one action."
    if turn["status"] != "continue" and actions:
        return {}, f"status {turn['status']} takes no actions."
    if turn["status"] in ("needs_input", "blocked", "complete") and not turn["message"].strip():
        return {}, f"status {turn['status']} needs a message for the person."
    if turn["status"] == "continue" and not actions:  # hand-offs only: said, and handed over
        turn["status"] = "complete" if turn["message"].strip() else "continue"
    return turn, None
