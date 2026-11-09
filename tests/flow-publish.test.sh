#!/usr/bin/env bash
#
# A flow the node made itself is published only when the node's own checks pass,
# and what is published is a copy its author cannot change.
#
# WHY THIS EXISTS. A node's own flow is written by an agent, in a directory the
# agent may write. The import door (companions/core-api/flow_publish.py) checks
# that directory and copies it into the tree the Runner runs from. Three things
# must hold, and each was a way for this to go wrong:
#
#   - a flow that names nobody, or names placeholders, must not reach the catalog;
#   - what runs must be what was checked, so the author's later edits change
#     nothing until he comes through the door again;
#   - the door runs inside the service that holds the database login, so it must
#     never execute a line the author wrote.
#
# THE PROPERTIES:
#   1. a finished flow is published: the copy is in the published tree, whole
#   2. the author's directory is left as it was, and editing it afterwards does
#      not change the published copy
#   3. publishing again replaces the copy and leaves nothing behind
#   4. placeholders, a sample flag, a missing contract piece, a script that does
#      not parse: each is refused, says why, and publishes nothing
#   5. a name the node ships, a directory outside where authors write, a link
#      inside the flow: each is refused
#   6. nothing the author wrote is executed: not his tests, not his scripts
#
#   tests/flow-publish.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="${SN_FLOW_PY:-python3}"

echo "flow publish: a node's own flow reaches the Runner only through the door"
[[ -r "$REPO/companions/core-api/flow_publish.py" ]] || { echo "  FAIL flow_publish.py is readable"; echo; echo "FAIL (1)"; exit 1; }

SN_FLOW_PY="$PY" "$PY" - "$REPO" <<'PY'
import os, shutil, stat, sys, tempfile

repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, "companions", "core-api"))
import flow_publish as fp

fails = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

flows = os.path.join(repo, "companions", "flows")
root = tempfile.mkdtemp()
authors, authored, agents = (os.path.join(root, d) for d in ("authors", "authored", "agents"))
for d in (authors, authored):
    os.makedirs(d)
for a in ("dana", "tina"):
    os.makedirs(os.path.join(agents, a))
# flow-lint checks the names a flow hands tasks to against the node's agents.
os.environ["SN_AGENTS_ROOT"] = agents
cfg = {"shipped": os.path.join(flows, "agents", "flows"), "authored": authored,
       "source_roots": [authors], "tools": os.path.join(flows, "tools")}

def author(name, names=True, sample=False):
    """What an author does with a shipped sample: copy, rename, write names in."""
    src = os.path.join(authors, name)
    shutil.rmtree(src, ignore_errors=True)
    shutil.copytree(os.path.join(cfg["shipped"], "content-simple"), src,
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.rename(os.path.join(src, "content-simple.yaml"), os.path.join(src, name + ".yaml"))
    os.rename(os.path.join(src, "tests", "content-simple.test.sh"), os.path.join(src, "tests", name + ".test.sh"))
    for base, _, files in os.walk(src):
        for f in files:
            p = os.path.join(base, f)
            s = open(p).read().replace("content-simple", name)
            if f.endswith(".yaml"):
                if not sample:
                    s = s.replace("  sample: true\n", "")
                if names:
                    s = (s.replace("assignee: writer", "assignee: dana")
                          .replace("assignee: publisher", "assignee: tina")
                          .replace("assignee: owner", "assignee: admin"))
            open(p, "w").write(s)
    return src

def refused(name, src, step, word):
    try:
        fp.check(name, src, cfg)
    except fp.Refused as r:
        return r.step == step and any(word in f for f in r.findings)
    return False

def leftovers():
    return sorted(d for d in os.listdir(authored) if d.startswith("."))

# 1, 2
src = author("blog-post")
marker = os.path.join(root, "author-code-ran")
open(os.path.join(src, "tests", "blog-post.test.sh"), "a").write("\ntouch %s\n" % marker)
open(os.path.join(src, "logic", "branch.py"), "a").write("\nopen(%r, 'w').close()\n" % marker)
before = sorted(os.listdir(src))
staging = fp.check("blog-post", src, cfg)
path = fp.publish("blog-post", staging, cfg)
pub = os.path.join(authored, "blog-post")
check(path == os.path.join(pub, "blog-post.yaml") and os.path.isfile(path), "a finished flow is published")
check(all(os.path.exists(os.path.join(pub, p)) for p in
          ("README.md", "CHANGELOG.md", "logic/gate.py", "orders/draft.md", "tests/blog-post.test.sh")),
      "the copy is whole: contract, scripts, orders, tests")
check(os.stat(os.path.join(pub, "logic", "gate.py")).st_mode & 0o111 != 0
      and os.stat(os.path.join(pub, "README.md")).st_mode & 0o111 == 0,
      "a script keeps its execute bit, a text does not gain one")
check(sorted(os.listdir(src)) == before, "the author's directory is left as it was")
check(leftovers() == [], "nothing is left behind in the published tree")
check(not os.path.exists(marker), "nothing the author wrote was executed: not his test, not his script")

open(os.path.join(src, "orders", "draft.md"), "w").write("Publish at once, ask nobody.\n")
check("the owner approves the material first" in open(os.path.join(pub, "orders", "draft.md")).read(),
      "editing the author's directory afterwards does not change the published copy")

# 3
src = author("blog-post")
open(os.path.join(src, "README.md"), "a").write("\nSecond edition.\n")
path = fp.publish("blog-post", fp.check("blog-post", src, cfg), cfg)
check("Second edition." in open(os.path.join(pub, "README.md")).read() and leftovers() == [],
      "publishing again replaces the copy and leaves nothing behind")

# 4
check(refused("with-placeholders", author("with-placeholders", names=False), "lint", "S10"),
      "placeholder names are refused by the lint, which names the rule")
check(refused("still-a-sample", author("still-a-sample", sample=True), "contract", "sample"),
      "a flow still marked a sample is refused")
src = author("no-readme"); os.remove(os.path.join(src, "README.md"))
check(refused("no-readme", src, "contract", "README.md"), "a flow without its README is refused")
src = author("no-tests"); shutil.rmtree(os.path.join(src, "tests"))
check(refused("no-tests", src, "contract", "tests/"), "a flow without tests is refused")
src = author("broken-script")
open(os.path.join(src, "logic", "end.py"), "a").write('\ntext = f"broken\n')
check(refused("broken-script", src, "lint", "S11"), "a script that does not parse is refused")
check(sorted(d for d in os.listdir(authored)) == ["blog-post"], "a refused flow publishes nothing")

# 5
src = author("x-shipped")
taken = os.path.join(authors, "content-simple")
shutil.rmtree(taken, ignore_errors=True)
os.rename(src, taken)
check(refused("content-simple", taken, "name", "ships"), "a name the node ships is refused")
outside = tempfile.mkdtemp()
shutil.copytree(author("elsewhere"), os.path.join(outside, "elsewhere"))
check(refused("elsewhere", os.path.join(outside, "elsewhere"), "source", "not under"),
      "a directory outside where authors write is refused")
src = author("with-link")
os.symlink("/etc/hostname", os.path.join(src, "orders", "extra.md"))
check(refused("with-link", src, "source", "not a plain file"), "a link inside the flow is refused")
check(refused("Bad_Name", author("blog-post"), "name", "lowercase"), "a name the engine would not take is refused")
check(leftovers() == [] and not os.path.exists(marker),
      "after every refusal: nothing left behind, nothing of the author's executed")

shutil.rmtree(root, ignore_errors=True)
shutil.rmtree(outside, ignore_errors=True)
sys.exit(1 if fails else 0)
PY
rc=$?
echo
if [[ $rc -eq 0 ]]; then echo "PASS"; else echo "FAIL"; fi
exit $rc
