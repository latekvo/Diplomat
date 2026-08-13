"""Diplomat's platform-neutral Python runtime — everything below the UI.

The shared assets loader, PR triage, the agent run book, token accounting, the
agent spawner, and :mod:`~diplomat_runtime.szponthost`, which is what puts Diplomat
behind a SzpontNet node. Both front-ends run it: the Linux applet imports it
directly, and the macOS app's mesh node reaches it through
``SZPONTNET_HOST=diplomat_runtime.szponthost``.

Deliberately empty of side effects and of imports. Installing the host in-process is the
importer's call (``diplomat_app/__init__`` makes it; the macOS app spawns a node that
resolves it by name instead), and this file is on the import path of the stdlib-only node
daemon, which must not pay for a module it never uses.
"""
