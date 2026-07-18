"""P21/C4: hint augmenters print per-file diagnostic chatter to stderr only
when verbose=True — silent by default (no log files; the SQLite journal is
the record of what the system did)."""
from __future__ import annotations

from helpers import make_cfg
from mlo import hints as hintsmod


def _seed_photo(world, cfg, rel="a.jpg"):
    st = world["store"]
    st.index_upsert(rel, 100, "qh", 0, "seed")
    st.index_commit()


def test_augment_exif_library_silent_by_default(world, capsys):
    cfg = make_cfg(world, taxonomy={"Photos": (".jpg",)})
    _seed_photo(world, cfg)
    hintsmod.augment_exif_library(cfg, world["store"], [], {})
    assert capsys.readouterr().err == ""


def test_augment_exif_library_verbose_prints_each_file_to_stderr(world, capsys):
    cfg = make_cfg(world, taxonomy={"Photos": (".jpg",)})
    _seed_photo(world, cfg)
    hintsmod.augment_exif_library(cfg, world["store"], [], {}, verbose=True)
    err = capsys.readouterr().err
    assert "exif: a.jpg" in err
