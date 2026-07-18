"""The hierarchical router: content-derived Jellyfin placement, idempotence,
explicit-default language, never-guess. (v0.2 core.)"""
from __future__ import annotations

import os
from dataclasses import replace

from helpers import make_cfg
from mlo.config import Layout
from mlo.taxonomy import Hints, detect_language, route

SUBTYPES = {"whatsapp": "Video/WhatsApp", "anime": "Video/Anime",
            "screenshot": "Images/Screenshots", "audiobook": "Audio/Audiobooks"}


def cfg_subtypes(world):
    return make_cfg(world, taxonomy=ALL,
                    layout=replace(Layout(), subtypes=SUBTYPES))

VID = {"Video": (".mp4", ".mkv", ".avi", ".vob")}
AUD = {"Audio": (".mp3", ".flac", ".amr")}
PHO = {"Photos": (".jpg", ".png", ".heic", ".dng", ".kdc")}
DOC = {"Documents": (".pdf", ".txt")}
ALL = {**VID, **AUD, **PHO, **DOC}


def cfg_for(world):
    return make_cfg(world, taxonomy=ALL)


def n(*segs):
    return os.sep.join(segs)


# ── movies: meaningful groupings, not source dumps ───────────────────────────

def test_movie_routes_to_title_year_under_language(world):
    """Files moving INTO a movie home get the Jellyfin clean name, folder and
    file alike (user directive): release-scene dots and quality tags go."""
    cfg = cfg_for(world)
    r = route(cfg, n("I_Movies", "tamil-films", "Sivaji.The.Boss.(2007).mkv"))
    assert r.dest_relpath == n("Video", "Movies", "Tamil",
                               "Sivaji The Boss (2007)",
                               "Sivaji The Boss (2007).mkv")
    assert r.rule.startswith("route:movie:lang:token")


def test_movie_without_language_token_uses_explicit_default(world):
    cfg = cfg_for(world)
    r = route(cfg, n("stuff", "Inception (2010) 1080p.mkv"))
    assert r.dest_relpath == n("Video", "Movies", "Other",
                               "Inception (2010)", "Inception (2010).mkv")
    assert r.rule == "route:movie:lang:default"     # provenance, not implicit


def test_video_without_identity_is_not_guessed(world):
    cfg = cfg_for(world)
    assert route(cfg, n("G_Dashcam", "FILE200817-092247F.mp4")) is None


def test_personal_hint_routes_to_personal(world):
    """Personal-kind hints keep the immediate parent-dir grouping: a dashcam
    folder moves under Personal as a folder, it does not flatten into it."""
    cfg = cfg_for(world)
    r = route(cfg, n("Videos", "G_Dashcam", "FILE200817-092247F.mp4"),
              Hints(media_kind="personal"))
    assert r.dest_relpath == n("Video", "Personal", "G_Dashcam",
                               "FILE200817-092247F.mp4")
    # directly under a top dir there is no grouping to keep — flat is right
    r2 = route(cfg, n("Videos", "clip-from-phone.mp4"),
               Hints(media_kind="personal"))
    assert r2.dest_relpath == n("Video", "Personal", "clip-from-phone.mp4")


def test_everything_under_personal_root_stays(world):
    """Personal is pure human placement: no parse, no hint, no re-derivation
    may move anything under it — however it is nested, whatever it is named."""
    cfg = cfg_for(world)
    deep = n("Video", "Personal", "Trips", "2019", "clip.mp4")
    for hints in (None, Hints(media_kind="personal"),
                  Hints(media_kind="movie", year=2019)):
        r = route(cfg, deep, hints)
        assert (r.dest_relpath, r.rule) == (
            deep, "route:personal:already-placed")
    movieish = n("Video", "Personal", "Roja (1992).mkv")
    r = route(cfg, movieish)
    assert r.dest_relpath == movieish


def test_agent_hints_fill_missing_movie_identity(world):
    cfg = cfg_for(world)
    r = route(cfg, n("flat", "Roja.mkv"),
              Hints(media_kind="movie", language="Tamil", year=1992))
    assert r.dest_relpath == n("Video", "Movies", "Tamil", "Roja (1992)",
                               "Roja (1992).mkv")


def test_hint_year_confirms_and_strips_unparenthesized_year(world):
    """'Where Eagles Dare1968.avi': the L3 parser refuses the bare year, but
    an attested year that CONFIRMS it may be stripped for clean naming."""
    cfg = cfg_for(world)
    r = route(cfg, n("I_Movies", "Where Eagles Dare1968.avi"),
              Hints(media_kind="movie", language="English", year=1968))
    assert r.dest_relpath == n("Video", "Movies", "English",
                               "Where Eagles Dare (1968)",
                               "Where Eagles Dare (1968).avi")


# ── tv ───────────────────────────────────────────────────────────────────────

def test_episode_routes_to_series_season(world):
    cfg = cfg_for(world)
    r = route(cfg, n("english shows", "Friends.S05E14.The.One.mkv"))
    assert r.dest_relpath == n("Video", "TV_Shows", "English", "Friends",
                               "Season 05", "Friends.S05E14.The.One.mkv")


# ── music / photos ───────────────────────────────────────────────────────────

def test_music_keeps_album_context_under_language(world):
    cfg = cfg_for(world)
    r = route(cfg, n("E_NAS1", "tamil", "Roja", "Chinna Chinna Aasai.mp3"))
    assert r.dest_relpath == n("Audio", "Music", "Tamil", "Roja",
                               "Chinna Chinna Aasai.mp3")


def test_photo_with_exif_year(world):
    cfg = cfg_for(world)
    r = route(cfg, n("Photos", "G_Pixel8Pro", "PXL_001.jpg"), Hints(year=2026))
    assert r.dest_relpath == n("Images", "Photos", "2026", "PXL_001.jpg")


def test_photo_without_year_goes_to_unsorted(world):
    cfg = cfg_for(world)
    r = route(cfg, n("scans", "old-scan.png"))
    assert r.dest_relpath == n("Images", "Photos", "Unsorted", "old-scan.png")


def test_raw_photo_routes_like_any_photo(world):
    """RAW (.dng/.kdc) in the Photos bucket routes exactly like a jpg: EXIF year
    -> year folder, no year -> Unsorted, and already under photos_root it STAYS
    (idempotence, the C15 posture). Pins the needs-human RAW pile's repair."""
    cfg = cfg_for(world)
    with_year = route(cfg, n("Other", "Unsorted", "DCP_0001.kdc"), Hints(year=2009))
    assert with_year.dest_relpath == n("Images", "Photos", "2009", "DCP_0001.kdc")
    no_year = route(cfg, n("Other", "Unsorted", "IMG_1234.dng"))
    assert no_year.dest_relpath == n("Images", "Photos", "Unsorted", "IMG_1234.dng")
    placed = n("Images", "Photos", "2015", "IMG_9.dng")
    assert route(cfg, placed).dest_relpath == placed        # already home: stays


def test_backups_bucket_files_are_never_routed(world):
    """.crypt12 / .zip live in the Backups bucket (non-media): route() returns
    None, so reorganize leaves them put and they are never staged as junk. This
    is the safe behavior the WhatsApp-backup / personal-zip piles depend on."""
    cfg = make_cfg(world, taxonomy={**ALL, "Backups": (".zip", ".crypt12")})
    assert route(cfg, n("Other", "Unsorted", "msgstore.crypt12")) is None
    assert route(cfg, n("Backups", "I_OldHDD", "docs-2014.zip")) is None


# ── idempotence: correctly-placed files route to themselves ──────────────────

def test_idempotent_movie_tv_music_photo(world):
    cfg = cfg_for(world)
    placed = [
        n("Video", "Movies", "Tamil", "Sivaji The Boss (2007)",
          "Sivaji.The.Boss.(2007).mkv"),
        n("Video", "TV_Shows", "English", "Friends", "Season 05",
          "Friends.S05E14.mkv"),
        n("Audio", "Music", "Hindi", "Album", "track.mp3"),
    ]
    for rel in placed:
        r = route(cfg, rel)
        assert r is not None and r.dest_relpath == rel, rel


def test_idempotent_photo_year_folder_without_rereading_exif(world):
    cfg = cfg_for(world)
    rel = n("Images", "Photos", "2018", "IMG_20180902.jpg")
    r = route(cfg, rel)                       # no hints: EXIF not re-read
    assert r.dest_relpath == rel and r.rule == "route:photo:already-placed"
    # contradicting EXIF wins over the current folder
    r2 = route(cfg, rel, Hints(year=2017))
    assert r2.dest_relpath == n("Images", "Photos", "2017", "IMG_20180902.jpg")


# ── non-media and language detection ─────────────────────────────────────────

def test_non_media_has_no_content_route(world):
    cfg = cfg_for(world)
    assert route(cfg, n("Documents", "taxes", "itr.pdf")) is None


def test_sidecars_inside_other_media_roots_stay(world):
    """Found in the first real repair plan: album art inside Music/ was routed
    to Photos/Unsorted, ripping it from its album. A file inside another media
    type's root is that tree's business — it stays."""
    cfg = cfg_for(world)
    placed = [
        n("Audio", "Music", "Devotional", "AlbumArt_large.jpg"),
        n("Video", "Movies", "Tamil", "Roja (1992)", "poster.jpg"),
        n("Images", "Photos", "2019", "clip.mp4"),
    ]
    for rel in placed:
        r = route(cfg, rel)
        assert r.dest_relpath == rel, rel
        assert r.rule == "route:sidecar:already-placed"


def test_artist_album_trees_are_never_flattened(world):
    """Review C15: 'Audio/Music/A R Rahman/Roja/track.mp3' must stay exactly
    where it is — the earlier language-only guard flattened artist trees to
    Other/. ANYTHING under the music root is the library's business."""
    cfg = cfg_for(world)
    for rel in (n("Audio", "Music", "A R Rahman", "Roja", "01 Chinna.mp3"),
                n("Audio", "Music", "Compilations", "song.mp3"),
                n("Audio", "Music", "other", "track.mp3")):
        r = route(cfg, rel)
        assert r.dest_relpath == rel and r.rule == "route:music:already-placed"


def test_photo_albums_are_never_scattered(world):
    """Review C15: 'Images/Photos/Wedding 2019/img.jpg' stays; only two
    evidence-backed repairs move in-tree photos — Unsorted->year, and an EXIF
    year contradicting the year folder."""
    cfg = cfg_for(world)
    album = n("Images", "Photos", "Wedding 2019", "img_001.jpg")
    assert route(cfg, album).dest_relpath == album
    assert route(cfg, album, Hints(year=2019)).dest_relpath == album  # no contradiction rule for albums

    unsorted = n("Images", "Photos", "Unsorted", "scan.jpg")
    assert route(cfg, unsorted).dest_relpath == unsorted              # no year: stays
    r = route(cfg, unsorted, Hints(year=2004))
    assert r.dest_relpath == n("Images", "Photos", "2004", "scan.jpg")
    assert r.rule == "route:photo:exif-year"


def test_layout_roots_compare_case_insensitively(world):
    """Review C15: a config typed 'video/movies' against on-disk 'Video/Movies'
    must not re-arm every idempotence bug on Windows."""
    from dataclasses import replace
    cfg = cfg_for(world)
    lc = make_cfg(world, taxonomy=ALL,
                  layout=replace(cfg.layout, movies_root="video/movies",
                                 music_root="audio/music",
                                 photos_root="images/photos"))
    placed = [
        n("Video", "Movies", "Tamil", "Sivaji The Boss (2007)", "Sivaji.(2007).mkv"),
        n("Audio", "Music", "A R Rahman", "Roja", "track.mp3"),
        n("Images", "Photos", "2018", "IMG_1.jpg"),
    ]
    for rel in placed:
        assert route(lc, rel).dest_relpath == rel, rel


def test_specials_and_series_level_groupings_are_trusted(world):
    """Review C15-minor: 'Specials/' and any series-level grouping under the
    TV root stay; only a loose episode (root- or language-level) is foldered."""
    cfg = cfg_for(world)
    specials = n("Video", "TV_Shows", "English", "Friends (1994)", "Specials",
                 "Friends.S00E01.mkv")
    assert route(cfg, specials).dest_relpath == specials
    in_series = n("Video", "TV_Shows", "English", "Friends", "Friends.S05E14.mkv")
    assert route(cfg, in_series).dest_relpath == in_series   # trusted grouping
    loose = n("Video", "TV_Shows", "English", "Friends.S05E14.mkv")
    r = route(cfg, loose)
    assert r.dest_relpath == n("Video", "TV_Shows", "English", "Friends",
                               "Season 05", "Friends.S05E14.mkv")


def test_valid_hand_named_homes_are_never_second_guessed(world):
    """A structurally-valid existing home wins over the derived name:
    'Friends (1994)' series folders and movie folders whose names differ from
    the filename parse stay exactly where they are."""
    cfg = cfg_for(world)
    tv = n("Video", "TV_Shows", "English", "Friends (1994)", "Season 05",
           "Friends.S05E14.mkv")
    assert route(cfg, tv).dest_relpath == tv
    assert route(cfg, tv).rule == "route:tv:already-placed"

    movie = n("Video", "Movies", "Tamil", "Sivaji The Boss (2007)",
              "Sivaji.(2007).mkv")           # folder name != filename parse
    assert route(cfg, movie).dest_relpath == movie
    assert route(cfg, movie).rule == "route:movie:already-placed"

    # but a year MISMATCH is not a valid home — the file still routes out
    wrong = n("Video", "Movies", "Other", "Some Folder (1999)",
              "Inception (2010).mkv")
    r = route(cfg, wrong)
    assert r.dest_relpath != wrong


# ── C26: year-stuttered movie parents re-route to a clean home ───────────────

def test_year_stuttered_movie_parent_reroutes_to_clean_home(world):
    """The disease seen on the live library:
    Video\\Movies\\English\\(2004) Mystic River (2004) (2004) (2004) (2004)\\
      (2004) Mystic River (2004) (2004) (2004) (2004).avi
    The already-placed tolerance was too generous; it swallowed year-stuttered
    parents forever. Stutter detection falls through to the clean route."""
    cfg = cfg_for(world)
    stuttered = n("Video", "Movies", "English",
                  "(2004) Mystic River (2004) (2004) (2004) (2004)",
                  "(2004) Mystic River (2004) (2004) (2004) (2004).mkv")
    r = route(cfg, stuttered)
    assert r.rule.startswith("route:movie:")
    assert r.rule != "route:movie:already-placed"
    assert r.dest_relpath == n("Video", "Movies", "English",
                               "Mystic River (2004)", "Mystic River (2004).mkv")


def test_junk_tagged_movie_parent_stays_put(world):
    """C26 must NOT re-route junk-tagged-but-unstuttered folders (the deliberate
    tolerance the router documents — a mass rename would be churn the user
    rejected). Only the stutter disease is targeted."""
    cfg = cfg_for(world)
    junk_tagged = n("Video", "Movies", "English",
                    "Inception (2010) [1080p BluRay YIFY x264]",
                    "Inception.2010.1080p.BluRay.x264.YIFY.mkv")
    r = route(cfg, junk_tagged)
    assert r.rule == "route:movie:already-placed"
    assert r.dest_relpath == junk_tagged


def test_filename_language_tag_never_outranks_the_folder(world):
    """CRITICAL idempotence (the user's hard constraint): a correctly-placed
    Tamil movie whose FILENAME carries '.English.Subs' must not move."""
    cfg = cfg_for(world)
    rel = n("Video", "Movies", "Tamil", "Padayappa (1999)",
            "Padayappa.(1999).English.Subs.mkv")
    r = route(cfg, rel)
    assert r.dest_relpath == rel                    # the contract

    # and when a file is still folder-less, the language SEGMENT it sits under
    # beats the filename tag — it lands (clean-named) in Tamil, not English
    loose = n("Video", "Movies", "Tamil", "Padayappa.(1999).English.Subs.mkv")
    r2 = route(cfg, loose)
    assert r2.dest_relpath == n("Video", "Movies", "Tamil", "Padayappa (1999)",
                                "Padayappa (1999).mkv")
    assert "lang:segment:tamil" in r2.rule


def test_music_directly_under_language_folder_does_not_nest(world):
    """Idempotence: Audio/Music/Tamil/song.mp3 must not become
    Audio/Music/Tamil/Tamil/song.mp3."""
    cfg = cfg_for(world)
    rel = n("Audio", "Music", "Tamil", "song.mp3")
    r = route(cfg, rel)
    assert r.dest_relpath == rel and r.rule == "route:music:already-placed"
    # and with an album folder, likewise stable
    rel2 = n("Audio", "Music", "Tamil", "Roja", "track.mp3")
    assert route(cfg, rel2).dest_relpath == rel2


def test_new_music_placement_does_not_double_language(world):
    """A flat source file under a language-named dir gains exactly one
    language segment."""
    cfg = cfg_for(world)
    r = route(cfg, n("E_NAS1", "tamil", "song.mp3"))
    assert r.dest_relpath == n("Audio", "Music", "Tamil", "song.mp3")


# ── finer taxonomy: config-driven subtype routing (§7) ───────────────────────

def test_subtype_hint_routes_to_configured_sub_root(world):
    """A critic-assigned finer media_kind routes to its configured sub-root:
    WhatsApp video -> Video/WhatsApp (keeping its parent grouping), anime -> a
    flat Video/Anime (NOT foldered into TV Season), screenshot -> Images/
    Screenshots. The finer-type folder names live in config, not code."""
    cfg = cfg_subtypes(world)
    r = route(cfg, n("eSrc", "dump", "VID-20230101-WA0007.mp4"),
              Hints(media_kind="whatsapp"))
    assert r.dest_relpath == n("Video", "WhatsApp", "dump",
                               "VID-20230101-WA0007.mp4")
    assert r.rule == "route:subtype"
    r2 = route(cfg, n("eSrc", "Naruto S01E05.mkv"), Hints(media_kind="anime"))
    assert r2.dest_relpath == n("Video", "Anime", "Naruto S01E05.mkv")
    r3 = route(cfg, n("Pictures", "Screenshot_2020.png"),
               Hints(media_kind="screenshot"))
    assert r3.dest_relpath == n("Images", "Screenshots", "Screenshot_2020.png")


def test_subtype_routing_is_idempotent(world):
    cfg = cfg_subtypes(world)
    placed = n("Video", "WhatsApp", "dump", "VID-20230101-WA0007.mp4")
    r = route(cfg, placed, Hints(media_kind="whatsapp"))
    assert r.dest_relpath == placed and r.rule == "route:subtype:already-placed"


def test_unknown_media_kind_falls_through_to_normal_routing(world):
    """media_kind='movie' is not a configured subtype -> ordinary movie routing
    still applies (subtypes are additive, never a catch-all)."""
    cfg = cfg_subtypes(world)
    r = route(cfg, n("stuff", "Inception (2010).mkv"), Hints(media_kind="movie"))
    assert r.dest_relpath == n("Video", "Movies", "Other",
                               "Inception (2010)", "Inception (2010).mkv")


def test_subtype_hint_ignored_outside_media_buckets(world):
    """A finer media_kind on a non-media bucket (Documents) is ignored."""
    cfg = cfg_subtypes(world)
    assert route(cfg, n("Documents", "notes.pdf"),
                 Hints(media_kind="whatsapp")) is None


# ── full-path metadata: DVD rips / provenance dumps route by the FOLDER path ─

def test_pathmeta_dvd_rip_routes_out_of_provenance_dump(world):
    """The KANDUKONDAEN case: filename VTS_10_1.VOB is useless, but the path
    (Videos\\E_NAS1\\Movies\\Tamil\\<Title>\\) gives type/language/title, so it
    routes OUT of the provenance dump into the canonical Video\\Movies tree,
    keeping the original filename (both parts co-locate)."""
    cfg = cfg_for(world)
    a = route(cfg, n("Videos", "E_NAS1", "Movies", "Tamil",
                     "KANDUKONDAEN KANDUKONDAEN", "VTS_10_1.VOB"))
    assert a is not None
    assert a.dest_relpath == n("Video", "Movies", "Tamil",
                               "KANDUKONDAEN KANDUKONDAEN", "VTS_10_1.VOB")
    assert a.rule.startswith("route:movie:path")
    b = route(cfg, n("Videos", "E_NAS1", "Movies", "Tamil",
                     "KANDUKONDAEN KANDUKONDAEN", "VTS_10_2.VOB"))
    assert b.dest_relpath == n("Video", "Movies", "Tamil",
                               "KANDUKONDAEN KANDUKONDAEN", "VTS_10_2.VOB")


def test_pathmeta_is_idempotent_for_yearless_movie_folder(world):
    """A yearless movie folder already under Video\\Movies routes to itself
    (converges) — the path-derived route must not create a move loop."""
    cfg = cfg_for(world)
    placed = n("Video", "Movies", "Tamil", "KANDUKONDAEN KANDUKONDAEN",
               "VTS_10_1.VOB")
    assert route(cfg, placed).dest_relpath == placed


def test_pathmeta_no_movie_segment_drains_to_unsorted(world):
    """No 'Movies' segment -> path-derivation invents no movie. The clip is
    stranded in a WRONG media root (Videos\\), so it DRAINS to Video\\Unsorted (a
    shelf the scoped-drain rule gates) — never a fabricated Movies\\<title>."""
    cfg = cfg_for(world)
    r = route(cfg, n("Videos", "G_Phone2", "Dance Demo - Toddlers.mp4"))
    assert r.dest_relpath == n("Video", "Unsorted", "Dance Demo - Toddlers.mp4")
    assert r.rule == "route:video:unsorted"


# ── audio triage: not all audio is music (audioclass) ────────────────────────

def test_audio_voice_note_routes_to_personal_flat(world):
    """A WhatsApp voice note in a provenance dump -> Audio\\Personal, flat, no
    provenance leak (audioclass says it is not music)."""
    cfg = cfg_for(world)
    r = route(cfg, n("Audio", "I_SSD1", "AUD-20170731-WA0009.mp3"))
    assert r.dest_relpath == n("Audio", "Personal", "AUD-20170731-WA0009.mp3")
    assert r.rule == "route:audio:voice"
    assert "I_SSD1" not in r.dest_relpath


def test_audio_discourse_routes_to_spoken_word(world):
    cfg = cfg_for(world)
    r = route(cfg, n("Audio", "I_SSD1", "947_1587962782_En_Pani_953.mp3"))
    assert r.dest_relpath == n("Audio", "Spoken_Word", "Discourse",
                               "947_1587962782_En_Pani_953.mp3")
    assert r.rule == "route:audio:spoken"


def test_audio_junk_stays(world):
    cfg = cfg_for(world)
    r = route(cfg, n("Audio", "I_SSD1",
                     "24b8ed046bdcee57ac76b9381a1fc037.mp3"))
    assert r is None                          # audioclass junk -> stays


def test_audio_already_in_personal_stays(world):
    cfg = cfg_for(world)
    placed = n("Audio", "Personal", "2015", "AUD-20150602-WA0001.mp3")
    r = route(cfg, placed)
    assert r.dest_relpath == placed and r.rule == "route:audio:already-placed"


# ── mixed-language compilations: per-file language, no provenance leak ───────

def test_mixed_language_compilation_scatters_per_file_flat(world):
    """The gnarly case: a folder with NO language marker (a compilation /
    provenance dump) holds songs of DIFFERENT languages. Each song routes flat
    under ITS OWN per-file (critic-hinted) language — the folder name never
    leaks into the home, and the two songs go to different language trees."""
    cfg = cfg_for(world)
    tamil = route(cfg, n("Audio", "E_HDD2_Part1", "Break The Rules.mp3"),
                  Hints(language="Tamil"))
    assert tamil.dest_relpath == n("Audio", "Music", "Tamil", "Break The Rules.mp3")
    assert tamil.rule == "route:music:hint-flat"
    hindi = route(cfg, n("Audio", "E_HDD2_Part1", "Ramta jogi.mp3"),
                  Hints(language="Hindi"))
    assert hindi.dest_relpath == n("Audio", "Music", "Hindi", "Ramta jogi.mp3")
    assert "E_HDD2_Part1" not in tamil.dest_relpath
    assert "E_HDD2_Part1" not in hindi.dest_relpath


def test_music_path_language_still_keeps_album_context(world):
    """The flat-scatter is scoped to HINT-derived language: a music file whose
    PATH carries the language keeps its artist/album structure (unchanged)."""
    cfg = cfg_for(world)
    r = route(cfg, n("E_NAS1", "tamil", "Roja", "Chinna Chinna Aasai.mp3"))
    assert r.dest_relpath == n("Audio", "Music", "Tamil", "Roja",
                               "Chinna Chinna Aasai.mp3")
    assert r.rule.startswith("route:music:lang") and r.rule != "route:music:hint-flat"


# ── content-sniff false-carves: route by magic bytes, not extension ──────────

def test_sniff_false_carve_routes_to_media_holding_pen(world):
    """A false-carve (an extension with no bucket, but a content sniff that says
    'video') routes into the Video Unclassified holding pen, keeping its parent
    grouping — reclassification with evidence, no guess at the specific home."""
    cfg = cfg_for(world)
    r = route(cfg, n("Other", "Unsorted", "recovered_0012.swf"),
              Hints(content_kind="video"))
    assert r.dest_relpath == n("Video", "Unclassified", "Unsorted",
                               "recovered_0012.swf")
    assert r.rule == "route:sniff:video"
    # audio and image carves land in their own type's pen; a shallow path keeps
    # just the filename
    ra = route(cfg, n("Other", "dump", "beep.dat"), Hints(content_kind="audio"))
    assert ra.dest_relpath == n("Audio", "Unclassified", "dump", "beep.dat")
    ri = route(cfg, n("Other", "x.img"), Hints(content_kind="image"))
    assert ri.dest_relpath == n("Images", "Unclassified", "x.img")


def test_sniff_never_overrides_a_configured_extension(world):
    """Content is consulted ONLY when the extension yields no bucket: a .mp4
    (bucketed Video) with a contradicting content_kind still routes by
    extension, never through the sniff holding pen."""
    cfg = cfg_for(world)
    r = route(cfg, n("stuff", "Inception (2010).mp4"), Hints(content_kind="audio"))
    assert r.dest_relpath == n("Video", "Movies", "Other", "Inception (2010)",
                               "Inception (2010).mp4")


def test_sniff_carve_already_in_media_area_stays(world):
    """Conservatism + idempotence: an unbucketed carve already under a media top
    (Video/Audio/Images) is that tree's business and is never reshuffled — so a
    carve that moved into the pen converges (route == current) on the next run."""
    cfg = cfg_for(world)
    held = n("Video", "Unclassified", "Unsorted", "recovered_0012.swf")
    r = route(cfg, held, Hints(content_kind="video"))
    assert r.dest_relpath == held
    assert r.rule == "route:sniff:already-in-media-area"


def test_sniff_no_hint_leaves_carve_unrouted(world):
    """Sniffing is opt-in evidence: without a content_kind hint an unbucketed
    extension is still None (the file stays put), exactly as before."""
    cfg = cfg_for(world)
    assert route(cfg, n("Other", "Unsorted", "recovered_0012.swf")) is None


def test_detect_language_precedence_is_positional(world):
    cfg = cfg_for(world)
    # 1) first directory segment that IS a language name wins (path order)
    assert detect_language(cfg, n("hindi", "tamil", "song.mp3"))[0] == "Hindi"
    # 2) directory tokens beat filename tokens
    hit = detect_language(cfg, n("tamil films", "hindi.song.mp3"))
    assert hit[0] == "Tamil" and hit[1].startswith("lang:token")
    # 3) filename tokens only as a last resort
    hit = detect_language(cfg, n("albums", "hindi.song.mp3"))
    assert hit[0] == "Hindi" and hit[1].startswith("lang:file-token")


# ── Spoken_Word / Devotional / Unsorted drains + image triage (2026-07-08) ───

def test_audio_comedy_routes_to_spoken_word_comedy(world):
    """A stage-comedy artist (library convention via config) is spoken word, not
    a song -> Audio/Spoken_Word/Comedy, flat."""
    cfg = make_cfg(world, taxonomy=ALL,
                   audio_patterns={"comedy": (r"S Ve\.? Shekher",)})
    r = route(cfg, n("Audio", "E_NAS1", "S Ve. Shekher - Halwa.mp3"))
    assert r.dest_relpath == n("Audio", "Spoken_Word", "Comedy",
                               "S Ve. Shekher - Halwa.mp3")
    assert r.rule == "route:audio:comedy"


def test_audio_legacy_audiobooks_migrates_under_spoken_word(world):
    """The pre-Spoken_Word flat Audio/Audiobooks home is no longer 'already
    placed': a discourse there migrates under the Spoken_Word parent."""
    cfg = cfg_for(world)
    r = route(cfg, n("Audio", "Audiobooks", "947_1587962782_En_Pani_953.mp3"))
    assert r.dest_relpath == n("Audio", "Spoken_Word", "Discourse",
                               "947_1587962782_En_Pani_953.mp3")


def test_audio_devotional_song_bucket(world):
    """A bhajan/stotram is devotional regardless of language: Music/Devotional/
    <Lang> when known, Music/Devotional/Unsorted when not."""
    cfg = cfg_for(world)
    known = route(cfg, n("Audio", "E_NAS1", "tamil", "Thiruppugazh Bhajan.mp3"))
    assert known.dest_relpath == n("Audio", "Music", "Devotional", "Tamil",
                                   "Thiruppugazh Bhajan.mp3")
    blind = route(cfg, n("Audio", "E_NAS1", "GOVINDASHTAKAM.mp3"))
    assert blind.dest_relpath == n("Audio", "Music", "Devotional", "Unsorted",
                                   "GOVINDASHTAKAM.mp3")


def test_audio_lost_tag_song_to_music_unsorted(world):
    cfg = cfg_for(world)
    r = route(cfg, n("Audio", "E_NAS1", "Track  5.mp3"))
    assert r.dest_relpath == n("Audio", "Music", "Unsorted", "Track  5.mp3")
    assert r.rule == "route:music:unsorted"


def test_audio_numeric_carve_to_music_unsorted(world):
    """A titleless numeric/recovery carve is not a song -> Music/Unsorted, never
    placed by language in Music/Other."""
    cfg = cfg_for(world)
    r = route(cfg, n("Audio", "HDD2_Part2", "190184408-190185158_001.mp3"))
    assert r.dest_relpath == n("Audio", "Music", "Unsorted",
                               "190184408-190185158_001.mp3")
    assert r.rule == "route:music:unsorted"


def test_song_without_language_routes_to_music_unsorted(world):
    """C30: an unmatched song with no language attested and no devotional
    marker lands in Music\\Unsorted (honest 'we don't know' home), not in
    Music\\<default-language>\\ (pretending default is a language decision)."""
    cfg = cfg_for(world)
    # Non-provenance folder, no language, no devotional form
    r = route(cfg, n("Audio", "phone_dump", "some_random_title.mp3"))
    assert r.dest_relpath == n("Audio", "Music", "Unsorted",
                               "some_random_title.mp3")
    assert r.rule == "route:music:unsorted"


def test_audio_dump_song_routes_flat_no_provenance_leak(world):
    """A song directly under a wrong media root's provenance folder routes FLAT
    into Music\\Unsorted (C30) — the E_NAS1 folder must not leak into Music,
    and 'no language known' lands in the honest Unsorted home, not a default
    language shelf."""
    cfg = cfg_for(world)
    r = route(cfg, n("Audio", "E_NAS1", "mystery track.mp3"))
    assert "E_NAS1" not in r.dest_relpath
    assert r.dest_relpath == n("Audio", "Music", "Unsorted", "mystery track.mp3")
    assert r.rule == "route:music:unsorted"


def test_image_whatsapp_screenshot_ui(world):
    cfg = cfg_for(world)
    wa = route(cfg, n("Photos", "E_NAS1", "IMG-20200318-WA0009.jpg"))
    assert wa.dest_relpath == n("Images", "WhatsApp", "IMG-20200318-WA0009.jpg")
    ss = route(cfg, n("Photos", "G_Phone1", "Screenshot_20190104-101112.png"))
    assert ss.dest_relpath == n("Images", "Screenshots",
                                "Screenshot_20190104-101112.png")
    ui = route(cfg, n("Photos", "E_NAS1", "loading.png"))
    assert ui.dest_relpath == n("Images", "Graphics_Icons", "loading.png")


def test_image_name_year_from_epoch(world):
    """A 13-digit epoch-ms filename gives a year home even with no EXIF hint."""
    cfg = cfg_for(world)
    r = route(cfg, n("Photos", "E_NAS1", "1493582779771.jpg"))
    assert r.dest_relpath == n("Images", "Photos", "2017", "1493582779771.jpg")
    assert r.rule == "route:photo:name-year"


def test_image_homes_are_already_placed(world):
    cfg = cfg_for(world)
    for rel in (n("Images", "WhatsApp", "IMG-1.jpg"),
                n("Images", "Graphics_Icons", "icon.png"),
                n("Images", "Screenshots", "shot.png")):
        assert route(cfg, rel).dest_relpath == rel


def test_video_devotional_dvd_to_video_devotional(world):
    """A devotional DVD rip (no Movies segment) stranded in Videos/ names a
    home: Video/Devotional."""
    cfg = cfg_for(world)
    r = route(cfg, n("Videos", "E_NAS1", "Devotional Songs", "Kabir",
                     "VTS_01_4.VOB"))
    assert r.dest_relpath == n("Video", "Devotional", "VTS_01_4.VOB")
    assert r.rule == "route:video:devotional"


def test_music_dump_nested_provenance_stays_flat(world):
    """A dump song routes FLAT past provenance ancestors at ANY depth (no
    provenance folder leaks into Music); a genuine album parent is kept.
    With no language attested the destination is Music\\Unsorted (C30)."""
    cfg = cfg_for(world)
    # backup-named parent -> flat
    r = route(cfg, n("Audio", "E_NAS1", "HDD2Backup", "Ramta jogi.mp3"))
    assert r.dest_relpath == n("Audio", "Music", "Unsorted", "Ramta jogi.mp3")
    # deeper drive-prefixed provenance parent -> flat
    r2 = route(cfg, n("Audio", "E_NAS1", "sub", "G_Phone2", "song.mp3"))
    assert r2.dest_relpath == n("Audio", "Music", "Unsorted", "song.mp3")
    # a genuine album parent is kept; provenance ancestors are dropped
    r3 = route(cfg, n("Audio", "E_NAS1", "Roja", "Chinna.mp3"))
    assert r3.dest_relpath == n("Audio", "Music", "Unsorted", "Roja", "Chinna.mp3")


def test_album_art_inside_music_stays(world):
    """Album art inside the Music tree is that tree's business — the cross-type
    sidecar guard (C18) keeps it, so imgclass/date-drain never touch it."""
    cfg = cfg_for(world)
    for art in (n("Audio", "Music", "Tamil", "Roja", "cover.jpg"),
                n("Audio", "Music", "Hindi", "Album", "AlbumArt.png")):
        assert route(cfg, art).dest_relpath == art


def test_comic_routes_to_series_under_comics_root(world):
    """A comic (.cbr/.cbz) is its own library type: Comics/<Series> by the
    '<Series> - <Title>' convention; no series -> flat; already home -> stays."""
    cfg = make_cfg(world, taxonomy={**ALL, "Comics": (".cbr", ".cbz")})
    r = route(cfg, n("Other", "Unsorted", "Star Wars - Crimson Empire 01.cbr"))
    assert r.dest_relpath == n("Comics", "Star Wars",
                               "Star Wars - Crimson Empire 01.cbr")
    assert r.rule == "route:comic:series"
    # issue-marker convention: series is 'Asterix', not the whole issue title
    aster = route(cfg, n("Other", "Unsorted",
                         "Asterix -01- Asterix the Gaul - 1961.cbr"))
    assert aster.dest_relpath == n("Comics", "Asterix",
                                   "Asterix -01- Asterix the Gaul - 1961.cbr")
    flat = route(cfg, n("Other", "Unsorted", "Watchmen.cbz"))
    assert flat.dest_relpath == n("Comics", "Watchmen.cbz")
    placed = n("Comics", "Star Wars", "Star Wars - Vector.cbr")
    assert route(cfg, placed).dest_relpath == placed


# ── ebooks (P17/C43): identity, not extension ─────────────────────────────────

EBOOKS = {"Ebooks": (".epub", ".mobi", ".lit")}


def cfg_ebooks(world):
    return make_cfg(world, taxonomy={**ALL, **EBOOKS})


def test_book_routes_to_author_series_from_filename_parse(world):
    """No hints: a pure filename parse resolves author + series -> shelved
    under 'Last, First'/Series/NN - Title."""
    cfg = cfg_ebooks(world)
    r = route(cfg, n("Documents", "Unsorted",
                     "Adams, Douglas - HGG 1 - Life, the Universe.epub"))
    assert r.dest_relpath == n("Books", "Adams, Douglas", "HGG",
                               "01 - Life, the Universe.epub")
    assert r.rule == "route:book:author-series"


def test_book_routes_to_author_only_no_series(world):
    cfg = cfg_ebooks(world)
    r = route(cfg, n("Other", "Unsorted", "Frank Herbert - Dune.mobi"))
    assert r.dest_relpath == n("Books", "Herbert, Frank", "Dune.mobi")
    assert r.rule == "route:book:author"


def test_book_hints_beat_filename_parse(world):
    """A hints book_author OVERRIDES whatever the filename would parse —
    identity from evidence (embedded metadata / a subagent judgment) always
    outranks the pure filename fallback."""
    cfg = cfg_ebooks(world)
    hints = Hints(book_author="Le Guin, Ursula K.", book_title="A Wizard of Earthsea",
                  book_series="Earthsea", book_index=1)
    r = route(cfg, n("Documents", "Unsorted", "some garbage name 1234.epub"), hints)
    # Windows forbids a trailing dot on a path segment; safe_segment strips it
    # (the same sanitizer every destination segment passes through).
    assert r.dest_relpath == n("Books", "Le Guin, Ursula K", "Earthsea",
                               "01 - A Wizard of Earthsea.epub")
    assert r.rule == "route:book:author-series"


def test_book_no_identity_goes_to_unsorted(world):
    """A title-only name with no author is never guessed — honest Unsorted."""
    cfg = cfg_ebooks(world)
    r = route(cfg, n("Other", "Unsorted", "AdventuresOfHuckleberryFinn.lit"))
    assert r.dest_relpath == n("Books", "Unsorted",
                               "AdventuresOfHuckleberryFinn.lit")
    assert r.rule == "route:book:unsorted"


def test_book_already_placed_under_books_root_stays(world):
    """A correctly-shelved book is that tree's business — never re-derived
    (C15/C18), even if its filename would parse differently now."""
    cfg = cfg_ebooks(world)
    placed = n("Books", "Herbert, Frank", "Dune.mobi")
    assert route(cfg, placed).dest_relpath == placed
    assert route(cfg, placed).rule == "route:book:already-placed"


def test_book_reshelve_from_unsorted_when_hints_arrive(world):
    """The narrow exception (comics series-normalize precedent): a file
    sitting in Books\\Unsorted whose hints NOW resolve an author gets
    re-derived into its real shelf home."""
    cfg = cfg_ebooks(world)
    hints = Hints(book_author="Twain, Mark")
    r = route(cfg, n("Books", "Unsorted", "AdventuresOfHuckleberryFinn.lit"),
             hints)
    assert r.dest_relpath == n("Books", "Twain, Mark",
                               "AdventuresOfHuckleberryFinn.lit")
    assert r.rule == "route:book:reshelve"


def test_book_unsorted_stays_unsorted_without_new_identity(world):
    cfg = cfg_ebooks(world)
    placed = n("Books", "Unsorted", "AdventuresOfHuckleberryFinn.lit")
    r = route(cfg, placed)
    assert r.dest_relpath == placed
    assert r.rule == "route:book:already-placed"


def test_book_title_colon_is_sanitized(world):
    """Embedded titles carry ':' (Windows-illegal) — the routing path has no
    existing sanitizer, so this is the integration-report finding this
    feature closes."""
    cfg = cfg_ebooks(world)
    hints = Hints(book_author="Rothfuss, Patrick",
                  book_title="The Wise Man's Fear: The Kingkiller Chronicle")
    r = route(cfg, n("Documents", "Unsorted", "book.epub"), hints)
    assert ":" not in r.dest_relpath
    assert r.dest_relpath == n(
        "Books", "Rothfuss, Patrick",
        "The Wise Man's Fear The Kingkiller Chronicle.epub")
