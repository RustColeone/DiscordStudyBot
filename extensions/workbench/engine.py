"""The `workbench` engine: a coding task for the club, from the request to one change set you approve.

The creator's messages become tasks (or more work on the conversation's open task); everyone else gets a short,
friendly answer from a classmate and no access to any project. See kernel/flow.py for a task's life.
"""

from __future__ import annotations

import asyncio

from contracts import SHUTDOWN, Engine, EngineSpec, ModelError, RunYield, Session

from .kernel import flow, prompts
from .kernel.context import Context, SeatUnavailable, Suspend


class WorkbenchEngine(Engine):
    spec = EngineSpec("workbench", "A club of classmates on a coding task: routed to one model, a cascade or a "
                      "critique, worked in its own git worktree, tested, and landed only after your approval.", "many")

    def __init__(self, board, registry, team, settings, link=None, prices=None):
        self.board, self.registry, self.team, self.settings = board, registry, team, settings
        self.link = link      # task → its page in the workbench (for conversations elsewhere, such as Discord)
        self.prices = prices  # the price list, fetched again on use once it is a day old

    async def run(self, session: Session) -> RunYield:
        ctx = Context(session, self.board, self.team, self.settings, self.link, self.prices)
        ready = {seat.persona: ctx.able(seat) for seat in self.team.seats.values()}
        if ready != self.team.ready:  # who has a key, for the page's crew picker (only runs can ask the core)
            self.team.ready = ready
            self.board.put("ready", ready)
        if session.access != "creator":
            return await self.member(ctx)
        if self.prices is not None:
            await self.prices.refresh_if_stale()
        try:
            return await flow.run(ctx, self.registry.all())
        except Suspend:
            if ctx.owns():
                ctx.update(phase="waiting for you")
            return RunYield.WAIT_APPROVAL
        except asyncio.CancelledError as error:
            if SHUTDOWN in error.args:  # the core is stopping: this run resumes on the next start, as it was
                raise
            if ctx.task_id and ctx.owns():
                ctx.update(status="cancelled", phase="")
                self.board.record_outcome(ctx.task_id, "cancelled")
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, (flow.Stopped, ModelError)) else f"{type(error).__name__}: {error}"
            if ctx.task_id and ctx.owns():
                ctx.update(status="failed", phase="", data={"failure": reason[:1000]})
                self.board.record_outcome(ctx.task_id, "stopped", reason=reason[:500])
            await ctx.say("stopped", flow.narrator(ctx), prompts.line("stopped", ctx.language, reason=reason[:500]))
            return RunYield.DONE

    async def stopped(self, session: Session, reason: str) -> None:
        """Cancelled while it waited for an approval: the task stops showing as worked on (its steps close too)."""
        ctx = Context(session, self.board, self.team, self.settings, self.link, self.prices)
        if ctx.task_id and ctx.owns() and (ctx.task().get("status") not in ("cancelled", "landed", "discarded")):
            ctx.update(status="cancelled", phase="")
            self.board.record_outcome(ctx.task_id, "cancelled")

    async def member(self, ctx: Context) -> RunYield:
        """Not the creator: a classmate chats, with no tools and no projects."""
        session = ctx.session
        lines = [f"{item.sender_name or ('you' if item.direction == 'out' else 'someone')}: {item.text}"
                 for item in session.history.recent(12) if item.message_id != session.trigger.id]
        for seat in self.team.candidates("router", ctx.present):
            system = prompts.MEMBER.format(voice=seat.voice, person=ctx.person)
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": ("Earlier messages:\n" + "\n".join(lines[-12:]) + "\n\n" if lines
                                                     else "") + f"{ctx.person}: {session.trigger.text}"}]
            try:
                answer = await ctx.model("member.m", seat, "cheap", messages, effort="low", max_tokens=800,
                                         timeout_s=60)
            except SeatUnavailable:
                continue
            await ctx.say("member.say", seat, answer["text"])
            return RunYield.DONE
        return RunYield.DONE
