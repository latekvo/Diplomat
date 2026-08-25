"""The host behind a Tor test — a log sink, nothing more.

A node with no host discards everything it narrates, which is right for a library
and useless for a test: a Tor link that never forms says so ("Mesh/Tor: …") in
exactly the stream that is being thrown away, so the failure arrives as a bare
timeout with no cause attached.

This puts the narration on stderr, which ``tornet`` captures per node and prints in
the assertion message, and which pytest captures for a transport driven in-process.
It answers nothing else — the state directory still comes from ``SZPONTNET_DIR``,
and jobs still run through ``SZPONTNET_SPAWN`` — so a node started with it is the
same node in every respect a test could be about.

Reached out-of-process by a node, via ``SZPONTNET_HOST=tornet_host``, and in-process
by the ``tornet`` fixture, which registers :class:`LoggingHost` directly.
"""

from __future__ import annotations

import sys

from szpontnet.host import Host


class LoggingHost(Host):
    def log(self, action: str, detail: str) -> None:
        sys.stderr.write(f"[{action}] {detail}\n")
        sys.stderr.flush()


def host() -> Host:
    return LoggingHost()
