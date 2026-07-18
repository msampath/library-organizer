"""pathmeta.derive — full-path movie identity across the heterogeneous naming
conventions seen in the real library (DVD rips, junk/actor grouping folders,
UPPERCASE type/lang, deep provenance prefixes)."""
from __future__ import annotations

import os

from helpers import make_cfg
from mlo import pathmeta


def n(*segs):
    return os.sep.join(segs)


def test_kandukondaen_dvd_rip(world):
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Videos", "E_NAS1", "Movies", "Tamil",
                                "KANDUKONDAEN KANDUKONDAEN", "VTS_10_1.VOB"))
    assert pm.media_type == "movie"
    assert pm.title == "KANDUKONDAEN KANDUKONDAEN"
    assert pm.year is None


def test_junk_grouping_folder_above_title_is_ignored(world):
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Videos", "E_NAS1", "Movies", "Tamil",
                                "!_watched_!", "AVVAISHANMUGI", "VTS_05_1.VOB"))
    assert pm.media_type == "movie" and pm.title == "AVVAISHANMUGI"


def test_actor_grouping_folder_above_title(world):
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Videos", "E_NAS1", "Movies", "Tamil",
                                "RAJINI", "Siva", "VTS_03_1.VOB"))
    assert pm.title == "Siva"


def test_uppercase_type_and_language_with_deep_prefix(world):
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Videos", "E_NAS1", "BeaTB", "FINAL", "MOVIES",
                                "ENGLISH", "BASIC INSTINCT", "VTS_02_1.VOB"))
    assert pm.media_type == "movie" and pm.title == "BASIC INSTINCT"


def test_title_year_extracted(world):
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Movies", "English", "1408 (2007)", "1408.avi"))
    assert pm.media_type == "movie" and pm.title == "1408 (2007)" and pm.year == 2007


def test_video_ts_structure_folder_skipped(world):
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Videos", "src", "Movies", "Tamil", "Roja",
                                "VIDEO_TS", "VTS_01_1.VOB"))
    assert pm.title == "Roja"


def test_no_movie_type_segment_is_empty(world):
    """Personal clips, music mis-filed as video, and recovery carves have no
    'Movies' segment -> no path-derived movie identity (they stay put)."""
    cfg = make_cfg(world)
    for rel in (n("Videos", "G_Phone2", "Dance Demo - Toddlers.mp4"),
                n("Videos", "E_NAS1", "Devotional Songs", "Kabir", "VTS_01_4.VOB"),
                n("Videos", "E_NAS1", "2371556Cd01.flv")):
        pm = pathmeta.derive(cfg, rel)
        assert pm.media_type is None and pm.title is None


def test_file_directly_under_language_folder_has_no_title(world):
    """A file loose under the language folder has no title folder -> empty."""
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Videos", "E_NAS1", "Movies", "Tamil", "clip.mp4"))
    assert pm.media_type is None


def test_numeric_placeholder_and_holding_pen_are_not_titles(world):
    """Video\\Movies\\Unclassified\\ (1)\\VTS... must NOT re-home as a movie titled
    '(1)' or 'Unclassified' — a numeric placeholder is no title, so it stays for
    a critic (the whole-library dry-run caught this churning ~100 files)."""
    cfg = make_cfg(world)
    for rel in (n("Video", "Movies", "Unclassified", " (1)", "VTS_01_1.VOB"),
                n("Video", "Movies", "Unclassified", " (100)", "VTS_03_1.VOB"),
                n("Video", "Movies", "Other", "VTS_01_1.VOB")):
        assert pathmeta.derive(cfg, rel).media_type is None


def test_holding_pen_ancestor_leaves_file_alone(world):
    """A clip under a holding pen (Unclassified) stays there — even a
    letter-named subfolder ('cool1') must not promote it into the main tree."""
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Video", "Movies", "English", "Unclassified",
                                "cool1", "cool1.flv"))
    assert pm.media_type is None


def test_stray_numeric_subfolder_under_real_title_consolidates(world):
    """A '(1)' subfolder under a REAL title folder is skipped, so the file
    consolidates under the real movie title (not promoted, not churned)."""
    cfg = make_cfg(world)
    pm = pathmeta.derive(cfg, n("Video", "Movies", "English",
                                "Jack-Jack Attack (2005)", " (1)", "clip.avi"))
    assert pm.media_type == "movie" and pm.title == "Jack-Jack Attack (2005)"
    assert pm.year == 2005
