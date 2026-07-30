"""The library's environment namespace.

Every knob SzpontNet reads from the environment is ``SZPONTNET_<NAME>``, and every
read goes through :func:`get`, so that is a fact about the code rather than a
convention someone has to keep. The names are part of the spec — the conformance
tester sets them on a candidate node and has no other way to configure one — so a
knob spelled some other way here is a node that cannot be tested.

They used to be ``DIPLOMAT_MESH_<NAME>``, from when the node was a feature of the
application that first shipped it rather than a library, and those are still
honoured when the new spelling is unset. That fallback is not politeness. The
variable it exists for is ``SECRET``: a machine whose shell profile still exports
the old name would otherwise read no join token at all and come up as an *open*
mesh, which is worse than failing to start. Deleting the fallback is safe once no
environment anywhere still sets the old names — grepping a source tree is not
enough to know that, since the values live in people's shells.
"""

from __future__ import annotations

import os

_PREFIX = "SZPONTNET_"
_LEGACY_PREFIX = "DIPLOMAT_MESH_"


def get(suffix: str, default: str | None = None) -> str | None:
    """``SZPONTNET_<suffix>``, falling back to the pre-rename ``DIPLOMAT_MESH_<suffix>``.

    An empty value is a value: ``SZPONTNET_SECRET=""`` means "no token, deliberately"
    and must not fall through to whatever the old name still holds.
    """
    value = os.environ.get(_PREFIX + suffix)
    if value is not None:
        return value
    return os.environ.get(_LEGACY_PREFIX + suffix, default)
