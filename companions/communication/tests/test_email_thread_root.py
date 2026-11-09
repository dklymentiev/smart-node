"""A letter in a chain is filed under the root of the chain, not under its parent.

On a node in the field (2026-10-20) the owner wrote to one agent four times about one
subject: 18:04, 18:25, 18:30 ("Re: Fwd: ..."). Every letter carries the whole chain in
`References` and names the letter it answers in `In-Reply-To`. The thread handle was
taken from `In-Reply-To`, the immediate PARENT, so each reply opened a new thread: four
letters, four `thread_id`s, two rows in each. The handle is also the key of the agent's
conversation session, so every reply of the owner started a conversation the agent had
no memory of, and `inbox_read(thread_id)` returned two letters of four.

The properties:

  1. the thread handle is the first id of `References` (the root of the chain)
  2. a letter without `References` falls back to `In-Reply-To`, then to its own id
  3. the three letters of the real chain, taken from the stored headers, share one handle
  4. angle brackets and the line breaks inside `References` do not change the handle
"""
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))

from adapters.email import adapter  # noqa: E402

ROOT = "thread-1-0000000000@mail.example"


def _msg(message_id, in_reply_to="", references=""):
    return {"message_id": "<%s>" % message_id, "in_reply_to": in_reply_to, "references": references}


def test_a_reply_is_filed_under_the_root_of_the_chain():
    refs = ("<%s>\r\n <thread-2-0000000000@mail.example> "
            "<179252980507.2268.2620029341102546837@node.test>" % ROOT)
    msg = _msg("thread-3-0000000000@mail.example",
               in_reply_to="<179252980507.2268.2620029341102546837@node.test>", references=refs)
    assert adapter._thread_id(msg) == ROOT


def test_without_references_the_parent_is_used_then_the_letter_itself():
    assert adapter._thread_id(_msg("b@x", in_reply_to="<a@x>")) == "a@x"
    assert adapter._thread_id(_msg("b@x")) == "b@x"


def test_the_three_letters_of_the_real_chain_share_one_handle():
    tail = ["<thread-2-0000000000@mail.example>",
            "<179252980507.2268.2620029341102546837@node.test>",
            "<thread-3-0000000000@mail.example>",
            "<179253756821.2268.2993075310027403733@node.test>",
            "<thread-4-0000000000@mail.example>",
            "<179253894621.2268.17674380995908554647@node.test>"]
    first = _msg("m1", in_reply_to=tail[1], references="<%s>\r\n %s" % (ROOT, " ".join(tail[:2])))
    second = _msg("m2", in_reply_to=tail[3], references="<%s>\r\n %s" % (ROOT, " ".join(tail[:4])))
    third = _msg("m3", in_reply_to=tail[5], references="<%s>\r\n %s" % (ROOT, " ".join(tail[:6])))
    handles = {adapter._thread_id(m) for m in (first, second, third)}
    assert handles == {ROOT}, "each reply opened a thread of its own: %r" % handles


def test_brackets_and_line_breaks_do_not_change_the_handle():
    a = _msg("x@y", references="<%s>" % ROOT)
    b = _msg("x@y", references="\r\n   <%s>\r\n <other@z>" % ROOT)
    assert adapter._thread_id(a) == adapter._thread_id(b) == ROOT
