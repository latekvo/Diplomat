# docs/

Project documentation.

## SzpontNet — the LAN peer-to-peer resource-sharing protocol

[**`szpontnet/`**](szpontnet/README.md) is the normative specification for
**SzpontNet**: the leaderless LAN protocol that lets machines self-discover,
advertise the resources they have available, and hand work to the best-fit
machine — with automatic take-over when one drops. It is written so an independent
implementation, in any language, can join the same mesh and interoperate.

**Co-Maintainer Mesh** (in [`linux/co_maintainer/mesh/`](../linux/co_maintainer/mesh)) is
this repository's reference implementation of SzpontNet; the shared constants are
in [`core/mesh.json`](../core/mesh.json).

Start at [`szpontnet/README.md`](szpontnet/README.md); the chapters are ordered for
bottom-up implementation.
