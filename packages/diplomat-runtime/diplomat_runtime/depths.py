"""A prompt model's rigor ladder — the ``depths`` list a wizard renders as a slider.

Two of the prompt models carry one (``review.json`` and ``issues.json``) and both
want the same four answers off it: the levels, their ids, the level a stored id
names, and which level is the default. That is one behaviour over two assets, so it
lives here once, parameterised by the loader — the Python twin of ``PromptDepth`` /
``ReviewCatalog`` / ``IssueCatalog`` in DiplomatCore.
"""

from __future__ import annotations

from collections.abc import Callable


class DepthLadder:
    """The depth ladder of one prompt model. ``load`` returns that model's parsed
    asset (``core.review`` / ``core.issues``) and is called per access, so an edited
    asset is picked up without a restart — the same way every other core read works.
    """

    def __init__(self, load: Callable[[], dict]) -> None:
        self._load = load

    def all(self) -> list[dict]:
        return self._load()["depths"]

    def ids(self) -> list[str]:
        return [d["id"] for d in self.all()]

    def default_id(self) -> str:
        return self._load().get("defaultDepth") or self.ids()[0]

    def by_id(self, depth_id: str) -> dict:
        """The level ``depth_id`` names, falling back to the ladder's default and
        then to its first — so a depth id that no longer exists (a renamed level, a
        config saved by an older build) still resolves to a real fragment."""
        levels = self.all()
        for d in levels:
            if d["id"] == depth_id:
                return d
        default = self._load().get("defaultDepth")
        for d in levels:
            if d["id"] == default:
                return d
        return levels[0]
