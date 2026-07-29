"""Diplomat — Linux (Qt6/PySide6) front-end.

A thin UI renderer over the shared, language-neutral ``core/`` assets
(GraphQL queries, tool catalog, filter constants, review-prompt fragments).
All the actual triage logic lives in ``core/`` and is shared verbatim with the
macOS SwiftUI app; only the rendering differs between platforms.
"""

__all__ = ["core", "gh", "models", "store", "review"]

# Put Diplomat behind any SzpontNet node running in this process. Done here rather
# than at each entry point because the cost of missing it is silent: the library's
# defaults are all valid answers, so a node would come up on the canonical v1 duty
# catalog, keep its state somewhere else and drop its log on the floor without a
# word. Importing this package is exactly the condition under which Diplomat is the
# host, so this is where it goes — and it is skipped, not failed, when the add-on
# isn't installed.
from . import szpont as _szpont  # noqa: E402 - after the docstring/__all__

if _szpont.AVAILABLE:
    from . import szponthost as _szponthost  # noqa: E402

    _szponthost.install()
