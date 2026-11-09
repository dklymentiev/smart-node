"""ensure_project is a sudo primitive, so its arguments are the boundary.

The op runs `sudo -n bash -c <template> _ ROOT SLUG GROUP MEMBERS...` as root.
The template itself is fixed, so the only way this becomes an arbitrary-root
primitive is through an argument that is not what it claims to be. These tests
are about that, and about the two properties the work area depends on:

  * 2770 setgid on the directory -- without setgid it works on the first day
    and silently stops working on the second, when the second member writes
  * the shared root stays 0755 root:root -- traversable by everyone, writable
    by nobody, so a project cannot be created by making a directory

The privileged half is not executed here: subprocess is replaced and what is
asserted is the exact argv that WOULD have been run. Running it for real is
the e2e's job, on a node, where the failing state can be constructed.
"""
import sys
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))


class _Proc:
    def __init__(self, rc=0, out="OK x", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


@pytest.fixture
def run_op(monkeypatch):
    """Call _ensure_project with subprocess stubbed; return (result, argv)."""
    import operations

    seen = {}

    def _run(argv, **kw):
        seen["argv"] = argv
        return _Proc(**seen.pop("proc", {})) if "proc" in seen else _Proc()

    monkeypatch.setattr(operations.subprocess, "run", _run)

    def _call(**kwargs):
        return operations._ensure_project(kwargs), seen.get("argv")
    return _call


# --------------------------------------------------------------------------
# What it asks root to do
# --------------------------------------------------------------------------

def operations_script():
    import operations
    return operations._ENSURE_PROJECT_SH


def test_the_group_and_path_follow_the_slug(run_op):
    result, argv = run_op(slug="acme-site")
    assert result["unix_group"] == "project-acme-site"
    assert result["fs_path"] == "/srv/projects/acme-site"
    # Flat, per no owner, department or workspace in the path.
    assert "/srv/projects/acme-site" == result["fs_path"]
    assert argv[:4] == ["sudo", "-n", "bash", "-c"]
    # argv is [sudo, -n, bash, -c, <script>, $0, ROOT, SLUG, GROUP, members...]
    assert argv[4] is operations_script(), "the script must be the fixed template"
    assert argv[5] == "_", "argv[5] is $0 for the -c script, not an argument"
    assert argv[6:9] == ["/srv/projects", "acme-site", "project-acme-site"]


def test_members_are_passed_as_separate_argv_entries(run_op):
    """Never interpolated into the script text. A member name reaching the
    shell as text is the difference between an argument and an injection."""
    _, argv = run_op(slug="acme", members=["agent-dev", "agent-tester"])
    assert argv[-2:] == ["agent-dev", "agent-tester"]


def test_the_template_sets_setgid_and_leaves_the_root_alone(run_op):
    """Read out of the script that root will run, not out of a docstring."""
    import operations
    sh = operations._ENSURE_PROJECT_SH
    assert 'chmod 2770 "$DIR"' in sh, "setgid is the whole mechanism"
    assert 'chmod 0755 "$ROOT"' in sh, "the shared root must not be group-writable"
    assert 'chown root:root "$ROOT"' in sh


def test_a_member_the_host_does_not_know_is_reported_not_swallowed(monkeypatch):
    """'I added them and it did not work' must have an answer in the response."""
    import operations
    monkeypatch.setattr(operations.subprocess, "run",
                        lambda argv, **kw: _Proc(err="SKIP unknown user agent-ghost\n"))
    out = operations._ensure_project({"slug": "acme", "members": ["agent-ghost"]})
    assert out["skipped"] == ["SKIP unknown user agent-ghost"]


def test_a_failed_run_raises_rather_than_reporting_success(monkeypatch):
    """The caller writes the registry row before calling this. If the disk half
    fails silently, the row claims a directory that does not exist."""
    import operations
    monkeypatch.setattr(operations.subprocess, "run",
                        lambda argv, **kw: _Proc(rc=1, err="groupadd: failure"))
    with pytest.raises(RuntimeError) as e:
        operations._ensure_project({"slug": "acme"})
    assert "groupadd: failure" in str(e.value)


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------

@pytest.mark.parametrize("slug", [
    "", "A", "1abc", "has space", "has/slash", "../escape", "x", "a" * 42,
    "semi;colon", "$(whoami)", "a`b`",
])
def test_a_slug_that_is_not_a_slug_is_refused(run_op, slug):
    """`trailing-` is deliberately NOT in this list: it matches, exactly as it
    does for an agent slug (_AGENT_SLUG_RE, same shape). A trailing hyphen is
    a legal group and directory name, and diverging from the agent rule for a
    cosmetic difference would mean two slug rules on one node."""
    with pytest.raises(ValueError):
        run_op(slug=slug)


@pytest.mark.parametrize("group", ["root", "sudo", "smart-node"])
def test_an_arbitrary_group_name_is_shape_checked(run_op, group):
    """These particular names DO match the shape and are accepted -- the check
    is on shape, not on a denylist, and this test records that honestly rather
    than implying a protection that is not there. What stops a caller choosing
    `root` is that only core-api calls this op, behind require_session."""
    result, _ = run_op(slug="acme", unix_group=group)
    assert result["unix_group"] == group


@pytest.mark.parametrize("group", ["Root", "with space", "a;b", "-lead"])
def test_a_malformed_group_name_is_refused(run_op, group):
    with pytest.raises(ValueError):
        run_op(slug="acme", unix_group=group)


@pytest.mark.parametrize("member", [
    "root; rm -rf /", "../../etc/passwd", "has space", "UPPER", "",
    "a" * 60,
])
def test_a_malformed_member_is_refused(run_op, member):
    with pytest.raises(ValueError):
        run_op(slug="acme", members=[member])


def test_an_omitted_group_falls_back_to_the_slug(run_op):
    """Empty means "not supplied" and takes the default. Raising here would
    make a caller that sends unix_group="" fail on a field it never set."""
    result, _ = run_op(slug="acme", unix_group="")
    assert result["unix_group"] == "project-acme"


def test_members_must_be_a_list_of_strings(run_op):
    with pytest.raises(ValueError):
        run_op(slug="acme", members="agent-dev")
    with pytest.raises(ValueError):
        run_op(slug="acme", members=[{"os_user": "agent-dev"}])


def test_the_op_is_registered_and_callable(run_op):
    """A handler nothing can reach is not a feature. It is registered
    non-dangerous on purpose: it is the same class of privilege as
    ensure_agent_users, which is also callable by the reconcile path."""
    import operations
    op = operations.REGISTRY["ensure_project"]
    assert op.handler is operations._ensure_project
    assert op.is_dangerous({}) is False
    assert "slug" in op.input_schema["properties"]
