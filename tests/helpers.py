"""Test-only helpers shared by pipeline tests."""
from __future__ import annotations

import os

from mlo.config import LLM, Config, Layout, Source


def make_cfg(world, **over) -> Config:
    base = dict(
        library_root=str(world["lib"]),
        sources=(Source("e", str(world["E"]), True),),
        staging={"E": os.path.join(str(world["E"]), "Delete"),
                 "I": os.path.join(str(world["I"]), "Delete")},
        protected_substrings=("bluestacks",),
        protected_drives=("C", "F"),
        junk_zero_byte=True,
        junk_names=("thumbs.db", "desktop.ini"),
        junk_extensions=(".tmp",),
        max_unmatched_pct=5.0,
        taxonomy={
            "Video": (".mp4", ".mkv"),
            "Audio": (".mp3",),
            "Documents": (".pdf", ".txt"),
        },
        layout=Layout(),
        llm=LLM(),
        config_hash="cfg-test",
        path="mlo.toml",
    )
    base.update(over)
    return Config(**base)
