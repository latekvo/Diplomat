"""The wire codec on its own: what one line is allowed to do to the node reading it."""

from szpontnet import protocol


def test_a_line_nested_past_the_parser_is_dropped_not_fatal():
    """``[[[[…`` deep enough to overflow json's decoder fits inside MAX_LINE_BYTES
    (on an interpreter with a fixed recursion limit, inside one beacon datagram), and
    what it raises is a RecursionError, not a decode error. Uncaught, it escaped every
    read loop: the accept path leaked the connection and the link pump tore down a
    healthy peer. The line is as long as the wire allows, so it overflows on every
    interpreter the node runs on."""
    deep = b"[" * (protocol.MAX_LINE_BYTES - 1) + b"\n"
    assert protocol.decode(deep) is None
