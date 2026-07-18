"""Enrichment connectors (§5.2) — opt-in, network, offline core preserved.

Every connector here FETCHES / PARSES / RENDERS. None writes a file: writing
metadata into the library (a .nfo, a poster, an embedded ID3/EXIF tag) is a
filesystem MUTATION and therefore the safety kernel's exclusive job — a gated
write op that is deliberately deferred (it also triggers hash-drift, see
mlo.hashdrift). So these modules never touch the kernel boundary, and a missing
key / offline / failed request degrades to a clean None or [] — the
deterministic core never crashes because a connector could not reach the net
(§5.4: network is never on the path of a destructive decision).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# Errors a connector swallows to degrade gracefully (never propagates to core).
NETWORK_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, OSError,
                  TimeoutError, ValueError, KeyError, json.JSONDecodeError)


def get_json(url: str, headers: dict, timeout: int) -> dict:
    """Default GET->JSON transport (stdlib). Injectable; tests pass a fake."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_bytes(url: str, headers: dict, timeout: int) -> bytes:
    """Default GET->bytes transport (posters, subtitle blobs)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
