"""imgclass — deterministic 'not all images are photos' triage, from the real
conventions in the 9,073-file Photos\\ provenance dump (35% WhatsApp, ~20%
web/UI graphics, 13% epoch-named, 2% screenshots)."""
from __future__ import annotations

from mlo import imgclass


def test_whatsapp():
    assert imgclass.classify("IMG-20200318-WA0009.jpg") == "whatsapp"
    assert imgclass.classify("IMG-20200212-WA0005.jpg") == "whatsapp"


def test_ui_graphics_by_gif_and_name():
    for name in ("loading.gif", "greyLineL.gif", "tab_note.gif",
                 "jog_tab_right_normal.png", "actionbar-logo.png",
                 "spinner.png", "btn_send_pressed.png", "app_icon.png"):
        assert imgclass.classify(name) == "ui", name


def test_screenshots():
    assert imgclass.classify("Screenshot_20190104-101112.png") == "screenshot"
    assert imgclass.classify("Screen Shot 2018-05-01.png") == "screenshot"


def test_ordinary_photo_is_none():
    for name in ("278082661-278082666_687.PNG", "04034_BG.jpg", "38391.jpg",
                 "DSC_0421.jpg"):
        assert imgclass.classify(name) is None, name


def test_epoch_ms_name_year():
    assert imgclass.name_year("1493582779771.jpg") == 2017     # 2017-04-30
    assert imgclass.name_year("1326496404926.png") == 2012


def test_name_year_rejects_non_timestamps():
    # a 10-digit id, a plain carve, and a real name give no year
    assert imgclass.name_year("1000109219.jpg") is None
    assert imgclass.name_year("278082661.PNG") is None
    assert imgclass.name_year("DSC_0421.jpg") is None


def test_structured_name_year_whatsapp_video_and_image():
    assert imgclass.structured_name_year("VID-20151015-WA0000.mp4") == 2015
    assert imgclass.structured_name_year("IMG-20200318-WA0009.jpg") == 2020


def test_structured_name_year_dashcam_leading_stamp():
    assert imgclass.structured_name_year("20250520050008_00001A.mp4") == 2025


def test_structured_name_year_rejects_unstructured_and_bogus_dates():
    assert imgclass.structured_name_year("random_clip.mp4") is None
    assert imgclass.structured_name_year("1493582779771.jpg") is None  # epoch-ms, not this pattern
    assert imgclass.structured_name_year("VID-20151399-WA0000.mp4") is None  # month 13
    assert imgclass.structured_name_year("VID-18991015-WA0000.mp4") is None  # year out of range


def test_config_extra_overrides_builtins():
    extra = {"ui": (r"^CDT\d+_",)}
    assert imgclass.classify("CDT70_BuildPreferences.png", extra) == "ui"
    assert imgclass.classify("CDT70_BuildPreferences.png") is None


def test_full_path_basename_is_used():
    assert imgclass.classify("Photos\\E_NAS1\\IMG-20200318-WA0009.jpg") \
        == "whatsapp"


def test_ui_installer_bitmaps():
    for name in ("setup.bmp", "Setup_2.bmp", "splash.bmp", "installer.png"):
        assert imgclass.classify(name) == "ui", name
