"""audioclass — deterministic 'not all audio is music' triage, from the real
conventions in a 1,097-file dump (75% WhatsApp voice, ~10% discourse)."""
from __future__ import annotations

from mlo import audioclass


def test_whatsapp_voice_and_recorder():
    assert audioclass.classify("AUD-20150602-WA0001.wma") == "voice"
    assert audioclass.classify("PTT-20160420-WA0022.opus") == "voice"
    assert audioclass.classify("New Recording 12.m4a") == "voice"
    assert audioclass.classify("Voice Memo 003.mp3") == "voice"


def test_spoken_discourse_series():
    assert audioclass.classify(
        "1006_1895639736_981_Telangana_Governments_Good_Gesture.mp3") == "spoken"
    assert audioclass.classify("947_1587962782_En_Pani_953.mp3") == "spoken"
    assert audioclass.classify(
        "405_526620954_Why_We_Hate_A_Few_En_Pani_710.mp3") == "spoken"


def test_junk():
    assert audioclass.classify("._12.ogg") == "junk"
    assert audioclass.classify("24b8ed046bdcee57ac76b9381a1fc037.1.aac") == "junk"
    assert audioclass.classify("fallbackring.ogg") == "junk"


def test_real_songs_are_song():
    for name in ("01 - Aaja Sanam - www.downloadming.com.mp3",
                 "01 Viriboni - Varnam - Bhairavi.wma",
                 "k.j.yesudas..thaye yesoda.3ga",
                 "Break The Rules.mp3"):
        assert audioclass.classify(name) == "song", name


def test_numeric_carve_is_none():
    assert audioclass.classify("190184408-190185158_001.OGG") is None
    assert audioclass.classify("12345.mp3") is None


def test_config_extra_overrides_builtins():
    extra = {"junk": (r"^Introducing Seagate",)}
    assert audioclass.classify("Introducing Seagate Backup.mp3", extra) == "junk"
    assert audioclass.classify("Introducing Seagate Backup.mp3") == "song"


def test_full_path_basename_is_used():
    assert audioclass.classify(
        "Audio\\I_SSD1\\AUD-20150602-WA0001.wma") == "voice"


def test_song_bucket_devotional():
    for name in ("VISHNU SAHASRANAMAM.mp3", "GOVINDASHTAKAM.mp3",
                 "HanumanChalisa.mp3", "abhang  govindha.3ga",
                 "dhanvantaristotram.mp3", "Sri Suprabhatam.mp3"):
        assert audioclass.song_bucket(name) == "devotional", name


def test_song_bucket_devotional_tamil_carnatic_starter():
    """C28: broad Tamil/Carnatic devotional starter — Tamil Vaishnavite/
    Shaivite poetry, Carnatic composition forms, Sanskrit composition suffixes,
    Bhagavad Gita, Dasa Sahitya. High-precision, form-based; not deity-based."""
    for name in (
            # Tamil Vaishnavite / Shaivite
            "andal--- paasuram.3ga", "pasurangal set 1.mp3",
            "thevaram vol 3.mp3", "thiruvasagam.mp3", "thiruvachagam padal 5.mp3",
            "Naalayira Divya Prabandham.mp3", "prabandham chapter 1.mp3",
            "tirupavai day 1.mp3", "thiruppavai margazhi.mp3",
            "andal recital.mp3", "Sri Thaniyan.mp3",
            # Carnatic composition forms
            "b.j..thillana.3ga", "MDR keerthanam.mp3", "krithi track 3.mp3",
            "swarajathi.mp3", "ragamalika medley.mp3", "varnam navaragamalika.mp3",
            # Sanskrit composition suffixes
            "gitagovinda ashtapadi 3.mp3", "purushasuktam full.mp3",
            "narayana suktham.mp3", "Sri Rudram.mp3", "Rudra kramam.mp3",
            "panchakshari mantra.mp3", "vishnu ashtottaram.mp3",
            "concluding mangalam.mp3",
            # Bhagavad Gita
            "Bhagavad Gita Chapter 2.mp3", "Bhagavath Githa Recitation.mp3",
            "Bhagavad Geeta.mp3", "gitopadesa.mp3",
            # Dasa Sahitya / Annamacharya
            "Kanaka Devarnama.mp3", "Purandara Devaranama.mp3",
            "Ugabhoga collection.mp3", "annamacharya sankirtanam.mp3",
    ):
        assert audioclass.song_bucket(name) == "devotional", name


def test_song_bucket_devotional_c29_dogfood_additions():
    """C29: named compositions & forms surfaced by the P7 live dogfood — Adi
    Shankara (Bhaja Govindam), Nammalvar (Thiruvoimozhi), Ramanuja (gadyam),
    Annamacharya (Nanati Baduku, sankirtana), Purandaradasa (Sriman Narayana),
    Sadasiva Brahmendra (Manasa Sancharare), Rajaji (Kurai Ondrum Illai),
    deity-marriage forms (kalyanam/parinayam), Warkari (Vittal), Harikatha
    tradition, numbered Bhagavad Gita chapters, hare-chant phrases."""
    for name in (
            "Bhaja Govindam.mp3",
            "Thiruvoimozhi.3ga", "Thiruvoimozhi2.3ga", "tiruvoymozhi vol 1.mp3",
            "Sri Venkatesa Gadyam.3ga", "saranagati gadyam Voice 044.3ga",
            "Nanati_Baduku.mp3", "NANATI_BADUKU.MP3", "Nanati Bratuku.mp3",
            "SrimanNarayana.mp3", "Sriman Narayana.mp3",
            "maanasa sancharare.3ga", "Manasa Sanchara.mp3",
            "KURAI ONDRUM ILLAI.mp3", "kurai  ondrum  illai.3ga",
            "padmavathi kalyanam.3ga", "radha kalyanam.3ga",
            "Sita Kalyana Vaibhogame.mp3",                # Tyagaraja
            "padmavathi parinayam.3ga", "rukmini parinay.3ga",
            "vittal maharaj.3ga", "Vitthal pandurang.mp3",
            "Visaka hari.3ga", "visakahari discourse.3ga",
            "harikatha kalakshepa.3ga",
            "Gita 1.3ga", "Gita 2.3ga", "Gita1.3ga", "Gita 047.3ga",
            "shri Krishna govinda hare murari.3ga",
            "hare krishna hare rama.3ga", "Hare Rama Bhajan.mp3",
            "Annamacharya sankirtana vol 1.mp3", "sankirtanam.3ga",
    ):
        assert audioclass.song_bucket(name) == "devotional", name


def test_song_bucket_no_false_positives_on_ordinary_music():
    """The starter list must not classify ordinary songs as devotional. Padam
    (Edith Piaf French chanson), Ilaiyaraaja film music, English pop, Western
    classical — none touch the devotional bucket."""
    for name in (
            "Padam Padam - Edith Piaf.mp3",             # French chanson
            "Ilaiyaraaja greatest hits.mp3",             # Tamil film music
            "Roja track 01.mp3", "AR Rahman soundtrack.mp3",
            "The Beatles Abbey Road.mp3", "Mozart Symphony 40.mp3",
            "Waka Waka Shakira.mp3",
            "College farewell song.mp3",
            "javali track.mp3",                         # form omitted (secular use)
    ):
        assert audioclass.song_bucket(name) != "devotional", name


def test_song_bucket_lost_tag():
    assert audioclass.song_bucket("Track  1.mp3") == "lost"
    assert audioclass.song_bucket("track 12.mp3") == "lost"
    assert audioclass.song_bucket("FILE002.mp3") == "lost"       # recovery carve


def test_song_bucket_ordinary_song_is_none():
    for name in ("054 - Guns n' Roses - Don't cry.mp3", "Break The Rules.mp3",
                 "k.j.yesudas..thaye yesoda.3ga"):
        assert audioclass.song_bucket(name) is None, name
