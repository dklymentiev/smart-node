"""Serve core-api on BOTH the TCP bind and a unix socket.

WHY A RUNNER AND NOT THE uvicorn CLI. `uvicorn` takes `--host/--port` or
`--uds`, never both, and running two processes would mean two of everything
in-process -- two connection pools, two caches, two of any future in-memory
state -- for what is one service. `uvicorn.Server.run(sockets=[...])` takes a
list, so one process answers on both doors and each request knows which one it
came through. That is the entire mechanism: on the socket the kernel names the
caller (SO_PEERCRED), and a caller cannot lie about a value it never sends.

HOW THE PERMISSION IS ARRANGED WITHOUT PRIVILEGE. Connecting to a unix socket
needs write permission on the socket AND traverse on every directory above it.
This service is deliberately not a member of `smart-node-agents`, so it cannot
chgrp the socket into that group. Instead systemd creates the directory
(RuntimeDirectory) and root gives it the group once at start; the directory is
SETGID, so the socket file inherits the group when this process binds it. Mode
0750 on the directory and 0660 on the socket then say exactly one thing: the
agents may call, nobody else may even see the path.

The TCP bind stays. Services (brin, voice, Studio) are not agents and are not
in the agent group; they keep the address they already use, and what changes
for them is nothing.
"""
import grp
import os
import socket
import sys

import uvicorn

HOST = os.environ.get("CORE_API_HOST", "")
PORT = int(os.environ.get("CORE_API_PORT", "8030"))
UDS = os.environ.get("CORE_API_UDS", "/run/smart-node/api/core-api.sock")
UDS_GROUP = os.environ.get("CORE_API_UDS_GROUP", "smart-node-agents")
BACKLOG = int(os.environ.get("CORE_API_BACKLOG", "2048"))


def _tcp_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(BACKLOG)
    s.set_inheritable(True)
    return s


def _unix_socket() -> socket.socket:
    d = os.path.dirname(UDS)
    os.makedirs(d, exist_ok=True)
    # The directory is 2750, owned by this service and group smart-node-agents.
    # systemd creates it (RuntimeDirectory) and gives it the group as root
    # (ExecStartPre), because this service is deliberately NOT a member of the
    # agent group and could not chgrp into it.
    #
    # SETGID is what makes 0660 on the socket work: a file created in a setgid
    # directory inherits the DIRECTORY's group, so the socket comes out
    # group-owned by smart-node-agents without this process holding any
    # privilege to make it so.
    try:
        st = os.stat(d)
        want = grp.getgrnam(UDS_GROUP).gr_gid
        if st.st_gid != want or not (st.st_mode & 0o2000):
            print(f"[serve] {d} is gid={st.st_gid} mode={oct(st.st_mode & 0o7777)}, "
                  f"expected group {UDS_GROUP} ({want}) with setgid -- agents will "
                  f"NOT be able to reach the socket until that is fixed",
                  file=sys.stderr)
    except KeyError:
        print(f"[serve] group {UDS_GROUP} does not exist on this node; "
              f"the socket will be unreachable by agents", file=sys.stderr)

    if os.path.exists(UDS):
        # A leftover file from a killed process refuses bind with EADDRINUSE and
        # the service would never come back after an unclean stop.
        os.unlink(UDS)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(UDS)
    s.listen(BACKLOG)
    os.chmod(UDS, 0o660)
    s.set_inheritable(True)
    return s


def main() -> None:
    config = uvicorn.Config(
        "main:app",
        log_level=os.environ.get("CORE_API_LOG_LEVEL", "info"),
        # h11 explicitly: peer credentials are read through the protocol object
        # that owns the ASGI receive callable, and pinning the implementation
        # keeps that one reach-through predictable across uvicorn versions.
        http="h11",
        # The event streams (/api/events/stream, /api/events/wakes) stay open as
        # long as a page or the Runner listens, and a graceful stop waits for every
        # open response: each restart hung 90 s until systemd killed the process,
        # and the node answered 502 meanwhile. Five seconds, then the streams are
        # cut; their clients reconnect on their own.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    server.run(sockets=[_tcp_socket(), _unix_socket()])


if __name__ == "__main__":
    main()
