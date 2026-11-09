"""Interactive `claude setup-token` over a PTY, bridged into two tool calls.

setup-token is an interactive TTY: it prints an OAuth URL ("Browser didn't open?
Use the url below...") then blocks on `Paste code here >`. We spawn it on a PTY with
a wide window (so the URL is not line-wrapped), capture the URL, hold the session,
and later write the pasted code to its stdin. Single in-flight session by design.
"""
import fcntl
import logging
import os
import pty
import re
import select
import signal
import struct
import termios
import threading
import time

_log = logging.getLogger("admin-runner.login")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB012]|\x1b[>=]|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip(b: bytes) -> str:
    return _ANSI.sub("", b.decode(errors="replace"))


class ClaudeLogin:
    def __init__(self):
        self._lock = threading.Lock()
        self._pid = None
        self._fd = None
        self._home = None

    def _read_until(self, needle: str, timeout: float):
        buf = b""
        t0 = time.time()
        while time.time() - t0 < timeout:
            r, _, _ = select.select([self._fd], [], [], 0.3)
            if r:
                try:
                    data = os.read(self._fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                if needle in _strip(buf):
                    return _strip(buf), True
        return _strip(buf), False

    def start(self, home: str, timeout: float = 40) -> dict:
        with self._lock:
            self._kill()
            os.makedirs(home, exist_ok=True)
            pid, fd = pty.fork()
            if pid == 0:
                os.environ["HOME"] = home
                os.environ["TERM"] = "xterm"
                os.execvp("claude", ["claude", "setup-token"])
                os._exit(127)
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
            except Exception:
                pass
            self._pid, self._fd, self._home = pid, fd, home
            clean, found = self._read_until("Pastecode", timeout)
            _log.info("login.start: prompt_found=%s buf_tail=%r", found, clean[-600:])
            urls = re.findall(r"https://claude\.com/\S+", clean)
            if not urls:
                self._kill()
                raise RuntimeError("claude setup-token did not emit an auth URL within timeout")
            return {"auth_url": urls[0]}

    def complete(self, code: str, timeout: float = 90) -> dict:
        with self._lock:
            if not self._pid or self._fd is None:
                raise RuntimeError("no login in progress -- call claude_login_start first")
            os.write(self._fd, code.strip().encode())
            time.sleep(0.3)
            os.write(self._fd, b"\r")
            buf = b""
            t0 = time.time()
            exited = False
            while time.time() - t0 < timeout:
                try:
                    wpid, _ = os.waitpid(self._pid, os.WNOHANG)
                    if wpid == self._pid:
                        exited = True
                except ChildProcessError:
                    exited = True
                r, _, _ = select.select([self._fd], [], [], 0.3)
                if r:
                    try:
                        data = os.read(self._fd, 4096)
                    except OSError:
                        break
                    if data:
                        buf += data
                if exited:
                    break
            cred = os.path.join(self._home, ".claude", ".credentials.json")
            tail = _strip(buf)
            m = re.search(r"sk-ant-oat\d+-[A-Za-z0-9_\-]{20,}", tail)
            token = m.group(0) if m else None
            token_found = token is not None
            cred_exists = os.path.exists(cred)
            ok = cred_exists or token_found
            # never let the printed token reach a log or output_tail (it IS returned
            # in the result for the caller to store server-side, then strip).
            red = re.sub(r"sk-ant-oat\d+-[A-Za-z0-9_\-]{20,}", "sk-ant-oat***REDACTED***", tail)
            _log.info("login.complete: ok=%s exited=%s token_found=%s cred_exists=%s tail=%r",
                      ok, exited, token_found, cred_exists, red[-400:])
            self._kill()
            return {"ok": ok, "credentials_path": cred if cred_exists else None,
                    "token_found": token_found, "token": token,
                    "output_tail": red[-400:]}

    def _kill(self):
        try:
            if self._pid:
                os.kill(self._pid, signal.SIGKILL)
                os.waitpid(self._pid, 0)
        except Exception:
            pass
        try:
            if self._fd is not None:
                os.close(self._fd)
        except Exception:
            pass
        self._pid = None
        self._fd = None


login = ClaudeLogin()
