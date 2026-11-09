#!/usr/bin/env bash
#
# sn-tool-install.test.sh -- the tool-install helper copies a tool's two files from its own
# project into /srv/tools/<name> and refuses everything else.
#
# Runs as an ordinary user against a scratch tree (the helper honours its test roots only for
# a non-root caller), so it needs no root and touches nothing on the machine.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SCRIPT="$ROOT/provision/sbin/sn-tool-install"

PASSED=0; FAILED=0
ok()  { echo "  ok   $1"; PASSED=$((PASSED + 1)); }
bad() { echo "  FAIL $1"; FAILED=$((FAILED + 1)); }

echo "tool-install helper"

if [[ ! -x "$SCRIPT" ]]; then
    bad "provision/sbin/sn-tool-install exists and is executable"
    echo "FAIL ($FAILED)"; exit 1
fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export SN_TOOL_INSTALL_WORK="$T/work" SN_TOOL_INSTALL_TOOLS="$T/tools" SN_TOOL_INSTALL_BACKUPS="$T/bak"
mkdir -p "$T/work" "$T/tools"

mkproj() {  # name
    mkdir -p "$T/work/$1/deploy/tool"
    printf '#!/usr/bin/env bash\necho hello\n' > "$T/work/$1/deploy/tool/$1.sh"
    printf '# %s\nusage\n' "$1" > "$T/work/$1/deploy/tool/CLAUDE.md"
}
run() { "$SCRIPT" "$@" >"$T/out" 2>"$T/err"; echo $?; }
refused() { [[ "$1" == "2" ]] && grep -q "sn-tool-install:" "$T/err"; }

mkproj demo
rc="$(run demo new)"
if [[ "$rc" == 0 && -x "$T/tools/demo/demo.sh" && -f "$T/tools/demo/CLAUDE.md" && -f "$T/tools/demo/.installed-by-sn-tool-install" ]] \
   && cmp -s "$T/work/demo/deploy/tool/demo.sh" "$T/tools/demo/demo.sh"; then
    ok "a tool is installed from its own project, with the marker"
else bad "a tool is installed from its own project, with the marker (rc=$rc)"; fi

grep -q "$(sha256sum "$T/tools/demo/demo.sh" | cut -d' ' -f1)" "$T/out" \
    && ok "the sums are printed for checking" || bad "the sums are printed for checking"

rc="$(run demo new)"
[[ "$rc" == 0 ]] && grep -q "^unchanged" "$T/out" \
    && ok "installing the same files again changes nothing" || bad "installing the same files again changes nothing (rc=$rc)"

printf 'echo changed\n' >> "$T/work/demo/deploy/tool/demo.sh"
rc="$(run demo new)"
refused "$rc" && ok "different files are refused without update" || bad "different files are refused without update (rc=$rc)"

rc="$(run demo update)"
if [[ "$rc" == 0 ]] && cmp -s "$T/work/demo/deploy/tool/demo.sh" "$T/tools/demo/demo.sh" && ls "$T"/bak/demo.* >/dev/null 2>&1; then
    ok "update replaces the files and keeps the old ones"
else bad "update replaces the files and keeps the old ones (rc=$rc)"; fi

mkdir -p "$T/tools/shipped"; : > "$T/tools/shipped/x"; mkproj shipped
rc="$(run shipped update)"
refused "$rc" && [[ ! -f "$T/tools/shipped/shipped.sh" ]] \
    && ok "a tool the installer ships (no marker) is never overwritten" || bad "a tool the installer ships (no marker) is never overwritten (rc=$rc)"

rc="$(run "../etc" new)"; refused "$rc" && ok "a name with a path in it is refused" || bad "a name with a path in it is refused (rc=$rc)"
rc="$(run Demo new)";     refused "$rc" && ok "a name outside the pattern is refused"  || bad "a name outside the pattern is refused (rc=$rc)"
rc="$(run ghost new)";    refused "$rc" && ok "a tool without a project is refused"    || bad "a tool without a project is refused (rc=$rc)"

mkproj linked; ln -sf /etc/passwd "$T/work/linked/deploy/tool/CLAUDE.md.tmp"; mv -f "$T/work/linked/deploy/tool/CLAUDE.md.tmp" "$T/work/linked/deploy/tool/CLAUDE.md"
rc="$(run linked new)"
refused "$rc" && [[ ! -e "$T/tools/linked" ]] && ok "a symlinked file is refused" || bad "a symlinked file is refused (rc=$rc)"

mkdir -p "$T/outside/deploy"; ln -s "$T/outside" "$T/work/escape"; mkdir -p "$T/outside/deploy/tool"
rc="$(run escape new)"
refused "$rc" && [[ ! -e "$T/tools/escape" ]] && ok "a project that is a link out of the tree is refused" || bad "a project that is a link out of the tree is refused (rc=$rc)"

mkproj broken; printf 'if then\n' > "$T/work/broken/deploy/tool/broken.sh"
rc="$(run broken new)"
refused "$rc" && ok "a script that does not parse is refused" || bad "a script that does not parse is refused (rc=$rc)"

mkproj big; head -c 300000 /dev/zero | tr '\0' 'x' > "$T/work/big/deploy/tool/CLAUDE.md"
rc="$(run big new)"
refused "$rc" && ok "an oversized file is refused" || bad "an oversized file is refused (rc=$rc)"

echo "passed: $PASSED, failed: $FAILED"
(( FAILED == 0 )) && echo PASS || { echo "FAIL ($FAILED)"; exit 1; }
