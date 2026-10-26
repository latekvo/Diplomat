"""The host behind a full-disk test node: its narration, appended to a file.

The file lives off the volume the test fills, so the lines a node writes about the
full disk are the ones the test reads. Named by ``FULLDISK_LOG``; reached via
``SZPONTNET_HOST=fulldisk_host``.
"""

from __future__ import annotations

import os

from szpontnet.host import Host


class FileLogHost(Host):
    def log(self, action: str, detail: str) -> None:
        with open(os.environ["FULLDISK_LOG"], "a", encoding="utf-8") as fh:
            fh.write(f"[{action}] {detail}\n")


def host() -> Host:
    return FileLogHost()
