"""The wire codec on its own: what one line is allowed to do to the node reading it."""

import sys

import pytest

from szpontnet import protocol


def test_a_line_nested_past_the_parser_is_dropped_not_fatal():
    """``[[[[…`` deep enough to overflow json's decoder fits inside MAX_LINE_BYTES (a
    thousand levels up to 3.11, ten thousand on 3.12, the C stack on 3.14), and what
    it raises is a RecursionError, not a decode error. Uncaught, it escaped every read
    loop: the accept path leaked the connection and the link pump tore down a healthy
    peer. The line is as long as the wire allows, so it overflows on every interpreter
    the node runs on."""
    deep = b"[" * (protocol.MAX_LINE_BYTES - 1) + b"\n"
    assert protocol.decode(deep) is None


def test_an_integer_past_the_parsers_digit_limit_is_dropped_not_fatal():
    """One integer literal longer than the interpreter's digit limit makes json.loads
    raise a bare ValueError - neither a decode error nor a RecursionError - from a
    line a few KiB long. Uncaught, it escapes every read loop like the deep line
    above. An interpreter without the limit parses any length, so it has no such
    line to drop."""
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not limit:
        pytest.skip("this interpreter has no integer digit limit")
    digits = b"9" * (limit + 1)
    assert protocol.decode(b'{"t":"heartbeat","n":' + digits + b"}\n") is None
