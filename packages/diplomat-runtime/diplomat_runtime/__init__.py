"""Diplomat's platform-neutral Python runtime — everything below the UI.

The shared assets loader, PR triage, the agent run book, token accounting and the
agent spawner. The Linux applet imports it directly; most of it has a Swift twin in
``diplomat-core``, which the macOS app links instead.

Deliberately empty of side effects and of imports.
"""
