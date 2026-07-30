"""What went wrong, told apart by whether retrying could ever help.

The library raises one :class:`szpontnet.ctl.CtlError` for every control-session
failure, so a caller that wants to react has to read the message to learn which
kind it got. This package splits that class along the only line that changes what
a caller should do:

* :class:`NodeUnavailable` - there was no node to talk to. Nothing was attempted,
  so nothing happened; start a node, or wait and retry.
* :class:`CommandRejected` - a node was there and the command did not take
  effect. Retrying the same command gets the same answer.

Both are also :class:`szpontnet.ctl.CtlError`, so code that already catches the
library's own exception keeps catching these unchanged - the split is additive.
"""

from __future__ import annotations

from szpontnet.ctl import CtlError


class SzpontError(Exception):
    """Base class for everything this package raises."""


class NodeUnavailable(SzpontError, CtlError):
    """No local node was reachable, so the command was never put to one.

    Raised when no node has ever run against this state directory, when the one
    that did is dead, when its snapshot names no usable control port, or when the
    socket to it fails. In every case the mesh saw nothing.
    """


class CommandRejected(SzpontError, CtlError):
    """A node was reached and the command did not take effect.

    Either it answered with an error - an unknown duty, a bad attribute, a
    missing API key - or it ended the control session without answering at all.
    The two are one class because they leave the caller in the same position: the
    node is up, and this command is not going to work as sent.
    """
