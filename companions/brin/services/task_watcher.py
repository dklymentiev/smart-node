"""TaskWatcher -- CRUTCH (temporary). Bridges node-planner task assignments to
the messages bus so an assignee agent gets woken, until the planner emits the
notification itself.

The intended design is for the planner to POST a webhook to
core-api /api/messages/inbound on assignment/handback (idempotent, destination-
agnostic), so the wake rides the single MessageWatcher path. The planner change
needs a planner image rebuild, deferred. Until then, THIS poller stands in for
the planner: it polls the node planner for actionable tasks assigned to an agent
and calls the SAME core-api webhook. Only this poller is throwaway; the webhook
and MessageWatcher routing are permanent.

Actionable = a task queued where "the assignee must act now" (the planner's
three statuses, migration 0010):
  - queued, no wait_reason  -> the assignee (e.g. project-manager) must pick it up.
  - queued for review       -> the assignee (e.g. secretary) must deliver the result.
De-dup is keyed (assignee, id, which of the two); a move or a new assignee is a
new key, so each hand-off wakes exactly once. The created system message is consumed
by MessageWatcher (it marks system doorbells processed), so nothing lingers.
"""

import asyncio
import logging
import os

import httpx
from core.service_proof import service_headers as _service_headers

logger = logging.getLogger("brin.task_watcher")

_POLL_INTERVAL = int(os.environ.get("TASK_POLL_INTERVAL", "15"))
_PLANNER_URL = os.environ.get("PLANNER_API_URL", "").rstrip("/")
_CORE_API_URL = os.environ.get("CORE_API_URL", "").rstrip("/")
_ACTOR = os.environ.get("TASK_WATCHER_ACTOR", "secretary")
# Only wake real agents (never a human assignee like a requester).
_AGENTS = set(
    a.strip() for a in
    os.environ.get("TASK_WAKE_AGENTS", "secretary,project-manager").split(",")
    if a.strip()
)
#: (what, wait_reason): new work nobody has taken, and work queued for review.
_QUEUES = (("new", None), ("review", "review"))


def _nudge(what, tid, title):
    if what == "review":
        return ("Task #%s is ready to deliver: %s. Deliver it to the requester "
                "per your tool contracts. Be concise." % (tid, title))
    return ("You have an open task #%s: %s. Pick it up and act per your tool "
            "contracts. Be concise." % (tid, title))


class TaskWatcher:
    """CRUTCH: polls the node planner, wakes an assignee via the messages bus."""

    def __init__(self):
        self._seen: set[str] = set()
        self._poll_task = None
        self._running = False

    async def start(self):
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("TaskWatcher started (interval=%ss, planner=%s, agents=%s)",
                    _POLL_INTERVAL, _PLANNER_URL, sorted(_AGENTS))

    async def stop(self):
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("TaskWatcher stopped")

    async def _poll_loop(self):
        while self._running:
            try:
                await asyncio.sleep(_POLL_INTERVAL)
                await self._check_tasks()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("TaskWatcher poll error")

    async def _check_tasks(self):
        for status, wait in _QUEUES:
            for t in await self._tasks(wait):
                assignee = t.get("assignee")
                tid = t.get("id")
                if assignee not in _AGENTS or tid is None:
                    continue
                if (t.get("wait_reason") or None) != wait:
                    continue
                key = "%s:%s:%s" % (assignee, tid, status)
                if key in self._seen:
                    continue
                title = (t.get("title") or "").strip() or ("task %s" % tid)
                logger.info("TaskWatcher: %s task #%s (%s) -> doorbell %s",
                            status, tid, assignee, assignee)
                if await self._doorbell(assignee, tid, status, title):
                    self._seen.add(key)

    @staticmethod
    def _headers():
        """From planner 0.4.0 the port needs a credential. Bound to the
        `runner` actor and non-delegating, so this reads but never authors under
        another name. Absent, the planner answers 401, the except below logs it
        and the watcher reports no tasks -- which is why the key is a unit-level
        EnvironmentFile rather than something this loop can be started without."""
        h = {"Remote-User": _ACTOR, "X-Status-Vocabulary": "new"}
        key = os.environ.get("PLANNER_API_KEY", "").strip()
        if key:
            h["X-API-Key"] = key
        return h

    async def _tasks(self, wait):
        """Queued tasks; with ``wait`` only those waiting for that reason. The
        planner has no filter for "no reason", so new work is read as all
        queued tasks and the caller keeps the ones with none."""
        status = "queued"
        url = "%s/api/v1/tasks?status_filter=queued&limit=%d" % (_PLANNER_URL, 100 if wait else 500)
        if wait:
            url += "&wait_reason=%s" % wait
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=self._headers())
                r.raise_for_status()
                data = r.json()
        except Exception:
            logger.exception("TaskWatcher: planner list failed (status=%s)", status)
            return []
        return data if isinstance(data, list) else []

    async def _doorbell(self, agent, tid, status, title):
        payload = {
            "recipient": agent,
            "content": _nudge(status, tid, title),
            "channel_type": "system",
            "metadata": {"task_id": tid, "status": status,
                         "source": "task-watcher-crutch"},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    "%s/api/messages/inbound" % _CORE_API_URL,
                    headers={"Content-Type": "application/json", **_service_headers()},
                    json=payload)
            if r.status_code == 200:
                return True
            logger.warning("TaskWatcher doorbell failed: status=%s body=%s",
                           r.status_code, r.text[:300])
            return False
        except Exception:
            logger.exception("TaskWatcher doorbell request error")
            return False
