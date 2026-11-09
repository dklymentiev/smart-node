"""Keep CLI system prompts out of process arguments and sudo's audit log."""

from contextlib import contextmanager
import os
import struct


PROMPT_STDIN_FLAG = "--sn-system-prompt-stdin"


@contextmanager
def prompt_file(prompt):
    """Provide an anonymous, private file inherited by a directly spawned CLI.

    There is no filesystem name to clean up or share with other agent users.
    The parent closes its copy after spawn, even on failure; the child's copy
    disappears when the child exits. sudo closes inherited descriptors, so its
    wrapper must create the file itself after the privilege drop instead.
    """
    if not prompt:
        yield [], ()
        return
    with os.fdopen(os.memfd_create("cli-system-prompt"), "w+b") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(prompt.encode("utf-8"))
        handle.flush()
        handle.seek(0)
        yield ["--system-prompt-file", f"/proc/self/fd/{handle.fileno()}"], (handle.fileno(),)


async def write_prompt_frame(process, prompt):
    """Write the wrapper's prompt frame before any stream-json user message.

    Length counts UTF-8 bytes, preserving newlines and non-ASCII text exactly.
    An early wrapper failure is reported through its exit code and stderr by
    the existing spawn monitor, just as a CLI startup failure is.
    """
    payload = prompt.encode("utf-8")
    try:
        process.stdin.write(struct.pack("!Q", len(payload)) + payload)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
