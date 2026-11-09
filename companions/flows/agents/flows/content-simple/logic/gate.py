#!/usr/bin/env python3
"""
content-gate: owner approval gate of the content-simple flow.

State machine:
  fresh (armed:content-gate absent):
    Post approval instructions comment, add blocked:approval + armed:content-gate,
    emit VERDICT: blocked.
  still-blocked (both tags present):
    Emit VERDICT: blocked (fallback; runner normally skips blocked tasks).
  resolved (armed:content-gate present, blocked:approval absent):
    Read the owner's decision as data: the tag decision:<approve|modify|reject>
    first, else the first line "DECISION: <what>" of a comment after the gate
    instructions. approve -> VERDICT: approved, modify -> VERDICT: revise,
    reject -> VERDICT: reject. No recorded decision -> re-block, VERDICT: blocked.

This block never moves the task. The hand-off after an approval (assignee
publisher) is declared in the flow's planner.on_block and written by the engine.

Context (stdin JSON from rein LogicRunner):
  output_file   -- path to write result JSON
  task_dir      -- rein task directory
  task_id       -- rein internal ID (not the planner task ID)
  task_input    -- dict of flow inputs; topic is at task_input["topic"]
  block_dir, outputs_dir, input_dir, depends_on, block_config, run_count

No httpx in venv. Uses stdlib http.client over unix socket.
No Remote-User header -- kernel establishes identity from socket.
Always exits 0 (rein treats non-zero return as script failure).
"""

import http.client
import json
import os
import re
import socket
import sys

SOCK = "/run/smart-node/api/core-api.sock"
GATE_MARKER = "Material-approval gate."
BLOCK_TAG = "blocked:approval"
ARMED_TAG = "armed:content-gate"

# The text names the one answer that works at a flow gate: a comment. It used
# to say "press Approve, Modify or Reject in the panel". The task drawer shows
# those buttons, and the planner's decision doors answer, only for a task in
# awaiting_approval; a task at this gate is in_progress with the tag
# blocked:approval, so the owner was sent to buttons that are not there.
#
# The first line of the request must end with "approval gate.": that is how the
# Runner knows a gate asked, and only an answer after it lifts the hold.
INSTRUCTIONS = (
    "Material-approval gate.\n\n"
    "The material is named in the preceding DRAFT: comment. Nothing is published yet.\n\n"
    "Answer with a comment on this task whose first line is one of:\n"
    "DECISION: approve\n"
    "DECISION: modify\n"
    "DECISION: reject\n"
    "Under modify or reject, write the note: for modify what to change, for "
    "reject why.\n\n"
    "The comment records the decision and lifts the block, so there is nothing "
    "else to do by hand.\n\n"
    "Removing the blocked:approval tag without recording a decision does not "
    "approve anything: the gate closes again and asks."
)


class UnixConn(http.client.HTTPConnection):
    """HTTP connection over a unix domain socket."""

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCK)


def api(method, path, body=None):
    """Make one planner request. Returns (status, parsed_body).

    With the Runner's own planner key in the environment (PLANNER_API_URL /
    PLANNER_API_KEY, passed down by the engine) the request goes straight to the
    planner as the runner. The core-api socket is the fallback: on 1.12.48 it
    refuses planner mutations from a non-agent process (403), which silently
    kept this gate from posting its instructions."""
    key = os.environ.get("PLANNER_API_KEY", "")
    base = os.environ.get("PLANNER_API_URL", "")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if key and base.startswith("http://"):
        host, _, port = base[len("http://"):].rstrip("/").partition(":")
        conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
        headers["X-API-Key"] = key
        path = path.replace("/api/planner", "", 1)
    else:
        conn = UnixConn("localhost")
    try:
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, {}
    except Exception as exc:
        # Log to stderr (rein captures it) and return a sentinel.
        print(f"GATE API ERROR | {method} {path} | {exc}", file=sys.stderr)
        return 0, {}
    finally:
        conn.close()


def get_tags(tid):
    st, data = api("GET", f"/api/planner/api/v1/tasks/{tid}/tags")
    if st == 200 and isinstance(data, list):
        return data
    print(f"GATE WARN | get_tags {tid} -> {st}", file=sys.stderr)
    return []


def add_tag(tid, tag):
    st, _ = api("POST", f"/api/planner/api/v1/tasks/{tid}/tags", {"tag": tag})
    if st not in (200, 201, 204):
        print(f"GATE WARN | add_tag {tag} -> {st}", file=sys.stderr)


def get_comments(tid):
    st, data = api("GET", f"/api/planner/api/v1/tasks/{tid}/comments")
    if st == 200 and isinstance(data, list):
        return data
    print(f"GATE WARN | get_comments {tid} -> {st}", file=sys.stderr)
    return []


def post_comment(tid, text):
    st, _ = api("POST", f"/api/planner/api/v1/tasks/{tid}/comments", {"content": text})
    if st not in (200, 201, 204):
        print(f"GATE WARN | post_comment -> {st}", file=sys.stderr)


def write_result(output_file, verdict, note):
    """Write the rein result JSON to output_file. Always called before return."""
    dirn = os.path.dirname(output_file)
    if dirn:
        os.makedirs(dirn, exist_ok=True)
    with open(output_file, "w") as fh:
        json.dump({"result": f"Material gate: {note}\nVERDICT: {verdict}"}, fh, indent=2)
    print(f"GATE | verdict={verdict} note={note!r}")


def extract_tid(topic):
    """Parse planner task ID from topic string 'Task #N: ...'."""
    m = re.match(r"Task\s+#(\d+)", topic or "", re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot extract task ID from topic: {topic!r}")
    return m.group(1)


def main():
    ctx = json.load(sys.stdin)
    output_file = ctx.get("output_file", "/tmp/gate-result.json")
    ti = ctx.get("task_input", {}) or {}
    # Runner-driven flows pass the planner task id as a first-class input; the
    # POST /run path instead embeds it in the topic as \"Task #ID: ...\". Prefer
    # the explicit input, fall back to parsing the topic.
    tid = str(ti.get("task_id") or "").strip()
    if not tid:
        try:
            tid = extract_tid(ti.get("topic", ""))
        except ValueError as exc:
            write_result(output_file, "blocked", f"ERROR: {exc}")
            return

    tags = get_tags(tid)
    armed = ARMED_TAG in tags
    blocked = BLOCK_TAG in tags

    if not armed:
        # First entry: arm the gate and request approval.
        post_comment(tid, INSTRUCTIONS)
        add_tag(tid, BLOCK_TAG)
        add_tag(tid, ARMED_TAG)
        write_result(output_file, "blocked", "Gate armed. Waiting for owner approval.")
        return

    if blocked:
        # Armed but still blocked: owner has not acted. Emit blocked again.
        # (Runner normally skips blocked tasks; this branch is a safety net
        # in case the gate is called while the tag is still present.)
        write_result(output_file, "blocked", "Still awaiting owner (blocked:approval tag present).")
        return

    # Armed, no longer blocked: owner removed the tag. Determine decision.
    # Scan only comments that appeared after the most recent gate instructions comment.
    comments = get_comments(tid)
    gate_idx = None
    for i in range(len(comments) - 1, -1, -1):
        if GATE_MARKER in (comments[i].get("content") or ""):
            gate_idx = i
            break
    recent = comments[gate_idx + 1:] if gate_idx is not None else comments

    # What the owner decided, read as a fact rather than guessed from prose.
    #
    # This used to be a substring scan: "reject" anywhere in any comment meant
    # reject, "revise" anywhere meant revise. A comment saying "do not reject
    # this, just fix the wording" therefore rejected the plan, and a comment
    # from anyone at all counted, not only from the person who was asked.
    #
    # The planner's decision endpoints (POST /tasks/{id}/approve|modify|reject)
    # now write the answer in one shape: a tag `decision:<what>` and a comment
    # whose FIRST line is `DECISION: <what>`. The tag is read first because it
    # cannot be ambiguous; the comment is the fallback for a decision recorded
    # by hand, and only its first line is inspected, so a note that mentions
    # another decision stays a note.
    decision = None
    for tag in tags:
        if tag.startswith("decision:"):
            word = tag.split(":", 1)[1].strip().lower()
            if word in ("approve", "modify", "reject"):
                decision = {"approve": "approved", "modify": "revise"}.get(word, "reject")
            break

    if decision is None:
        for c in recent:
            head = ((c.get("content") or "").strip().split("\n", 1)[0]).strip()
            if not head.upper().startswith("DECISION:"):
                continue
            word = head.split(":", 1)[1].strip().lower()
            if word in ("approve", "modify", "reject"):
                decision = {"approve": "approved", "modify": "revise"}.get(word, "reject")

    if decision is None:
        # The block was lifted but nobody recorded a decision. Do not assume one:
        # an approval that nobody gave is the failure this gate exists to prevent.
        post_comment(tid, GATE_MARKER + " no decision recorded; the gate stays closed. "
                          "Answer with a comment whose first line is "
                          "DECISION: approve, DECISION: modify or DECISION: reject.")
        add_tag(tid, BLOCK_TAG)
        write_result(output_file, "blocked", "Block lifted without a recorded decision.")
        return

    if decision == "revise":
        post_comment(tid, "Gate: changes requested by owner. Routing back to draft.")
        write_result(output_file, "revise", "Owner requested revisions.")

    elif decision == "reject":
        post_comment(tid, "Gate: material rejected by owner. Nothing is published. Flow terminated.")
        write_result(output_file, "reject", "Owner rejected the material.")

    else:
        post_comment(tid, "Gate: material approved by owner. Proceeding to publish.")
        # The hand-off to the publisher is planner.on_block publish.waiting; the
        # engine writes it. A PATCH here made this block a second writer.
        write_result(output_file, "approved", "Owner approved the material.")


if __name__ == "__main__":
    main()
