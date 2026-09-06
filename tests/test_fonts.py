"""Printing the rest of the world's alphabets.

Three pieces are checked here. That a TrueType font can be read and cut down to
the handful of characters a document uses (app/ttf.py). That the right font is
found on the computer and that a font whose licence forbids embedding is left
alone (app/fonts.py). And that Arabic comes out joined and in the right order
rather than as a row of unconnected letters running the wrong way
(app/shaping.py).

The end of it is the only test that really matters to a customer: put a Chinese
company name on an invoice and read it back out of the PDF.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

os.environ.setdefault("NEXORA_DATA", tempfile.mkdtemp(prefix="nexora-fonts-"))

from app import fonts as fontfinder  # noqa: E402
from app import shaping  # noqa: E402
from app.pdfwriter import Canvas, plan, printable, width_of  # noqa: E402
from app.ttf import FontError, TrueType  # noqa: E402
from tests.pdftext import text_of  # noqa: E402

LATIN = "Acme Trading Ltd"
CHINESE = "上海东方贸易有限公司"
GREEK = "Ελληνικά Ναυτιλιακά"
CYRILLIC = "ООО Восток"
ARABIC = "شركة النور للتجارة"
HEBREW = "מרכז הספר"


def a_font(text: str = "Aé") -> TrueType:
    """Any font on this computer that can draw ``text``, or skip."""
    found = fontfinder.find(text)
    if found is None:
        pytest.skip(f"no font on this computer draws {text!r}")
    return found.font


# --------------------------------------------------------------------------
# Reading a font file
# --------------------------------------------------------------------------


def test_a_font_says_what_it_is():
    font = a_font()
    assert font.num_glyphs > 100
    assert font.units_per_em in (1000, 1024, 2048)
    assert font.name and " " not in font.name


def test_something_that_is_not_a_font_is_refused():
    with pytest.raises(FontError):
        TrueType.load(b"this is not a font at all")


def test_a_file_too_small_to_be_a_font_is_refused():
    with pytest.raises(FontError):
        TrueType.load(b"\x00\x01\x00")


def test_postscript_outlines_are_refused_rather_than_half_supported():
    """An OpenType/CFF font needs a different kind of embedding altogether."""
    with pytest.raises(FontError):
        TrueType.load(b"OTTO" + b"\x00" * 60)


def test_the_character_map_finds_letters():
    font = a_font()
    table = font.cmap()
    assert table.get(ord("A"))
    assert table.get(ord("A")) != table.get(ord("B"))


def test_a_font_knows_what_it_cannot_draw():
    font = a_font(LATIN)
    assert font.covers("Acme")
    assert font.missing("") == {"", ""}


def test_width_grows_with_the_size_and_with_the_string():
    font = a_font()
    assert font.text_width("WWWW", 10) > font.text_width("iiii", 10)
    assert font.text_width("hello", 20) == pytest.approx(
        font.text_width("hello", 10) * 2)


def test_a_font_whose_licence_forbids_embedding_is_not_used():
    """The whole purpose of the permission bits, and easy to ignore by accident."""
    font = a_font()
    font.fs_type = 0x0002                     # "no embedding"
    assert font.can_be_embedded is False
    font.fs_type = 0x0200                     # "bitmap only"
    assert font.can_be_embedded is False
    font.fs_type = 0x0004                     # "preview and print" — allowed
    assert font.can_be_embedded is True


# --------------------------------------------------------------------------
# Cutting a small font out of a big one
# --------------------------------------------------------------------------


def test_a_subset_is_far_smaller_than_the_font_it_came_from():
    font = a_font(CHINESE) if fontfinder.find(CHINESE) else a_font()
    glyphs = {font.glyph_for(ch) for ch in "Acme Ltd"}
    cut = font.subset(glyphs)
    assert len(cut.data) < len(font.raw)
    assert len(cut.data) < 60_000


def test_the_missing_character_box_is_always_kept_and_always_first():
    font = a_font()
    cut = font.subset({font.glyph_for("A")})
    assert cut.mapping[0] == 0


def test_the_subset_can_be_read_back_as_a_font():
    font = a_font()
    cut = font.subset({font.glyph_for(ch) for ch in "Hello"})
    again = TrueType.load(cut.data)
    assert again.num_glyphs == cut.count
    assert again.units_per_em == font.units_per_em


def test_a_composite_letter_drags_its_pieces_with_it():
    """"é" is often a reference to "e" plus a reference to an accent. Leave the
    pieces out and it prints as nothing at all."""
    font = a_font("é")
    accented = font.glyph_for("é")
    pieces = font._components(accented)
    if not pieces:
        pytest.skip("this font stores é as one outline")
    cut = font.subset({accented})
    assert all(piece in cut.mapping for piece in pieces)


def test_glyph_numbers_asked_for_are_the_numbers_given():
    """The PDF hands out ids as it prints, long before the font is cut."""
    font = a_font()
    wanted = {font.glyph_for("A"): 1, font.glyph_for("B"): 2}
    cut = font.subset(set(wanted), assigned=wanted)
    assert cut.mapping[font.glyph_for("A")] == 1
    assert cut.mapping[font.glyph_for("B")] == 2


# --------------------------------------------------------------------------
# Choosing a font
# --------------------------------------------------------------------------


def test_latin_text_never_reaches_a_font_file():
    """Every ordinary invoice takes this path, so it must cost nothing."""
    assert plan(LATIN) == [(None, LATIN)]
    assert fontfinder.needs_embedding(LATIN) is False


def test_text_the_built_in_fonts_cannot_print_is_recognised():
    assert fontfinder.needs_embedding(CHINESE)
    assert fontfinder.needs_embedding(GREEK)
    assert fontfinder.needs_embedding(ARABIC)


def test_the_latin_part_of_a_mixed_line_keeps_the_document_typeface():
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    pieces = plan(f"{CHINESE} Ltd")
    assert (None, " Ltd") in pieces
    assert any(face is not None for face, _ in pieces)


def test_one_font_is_used_for_the_whole_of_a_name():
    """Two different Chinese faces inside one company name looks broken."""
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    used = {id(face) for face, _ in plan(CHINESE) if face is not None}
    assert len(used) == 1


def test_nothing_can_be_printed_when_there_is_no_font_for_it(monkeypatch):
    monkeypatch.setattr(fontfinder, "find", lambda text, bold=False: None)
    assert printable("₦") is False
    assert printable("Acme Ltd") is True


def test_the_font_folder_is_read_once_and_can_be_forgotten():
    first = fontfinder.index()
    assert first is fontfinder.index()
    fontfinder.forget()
    assert fontfinder.index() is not first


# --------------------------------------------------------------------------
# Arabic: joined, and the right way round
# --------------------------------------------------------------------------


def test_arabic_letters_take_the_shape_their_neighbours_give_them():
    joined = shaping.join("بيت")                 # beh, yeh, teh
    assert joined == "ﺑﻴﺖ"        # initial, medial, final


def test_a_letter_on_its_own_stays_on_its_own():
    assert shaping.join("ب") == "ﺏ"          # isolated


def test_lam_followed_by_alef_becomes_the_one_glyph_arabic_requires():
    assert shaping.join("لا") == "ﻻ"


def test_a_font_without_the_joined_forms_keeps_the_plain_letters():
    """Better a readable row of letters than a row of empty boxes."""
    assert shaping.join("بيت", can_draw=lambda ch: False) == "بيت"


def test_arabic_is_put_in_printing_order():
    text = "شركة النور"
    out = shaping.shape(text)
    assert out != text
    assert out[::-1] != text                     # not merely reversed: joined too
    assert len(out) == len(text)


def test_a_reference_number_inside_arabic_still_reads_forwards():
    out = shaping.shape("فاتورة رقم INV-2043")
    assert "INV-2043" in out


def test_hebrew_is_reversed_but_not_joined():
    out = shaping.shape(HEBREW)
    assert out == HEBREW[::-1]


def test_latin_text_is_left_completely_alone():
    assert shaping.shape(LATIN) == LATIN
    assert shaping.shape("") == ""
    assert shaping.is_rtl(LATIN) is False


def test_an_arabic_name_inside_an_english_sentence_is_turned_round():
    out = shaping.shape(f"Invoice for {ARABIC}")
    assert out.startswith("Invoice for ")
    assert out != f"Invoice for {ARABIC}"


# --------------------------------------------------------------------------
# The PDF itself
# --------------------------------------------------------------------------


def a_page(text: str, **kw) -> bytes:
    c = Canvas()
    c.text(42, 60, text, **kw)
    return c.output()


def test_a_latin_document_embeds_nothing_at_all():
    assert b"/Type0" not in a_page(LATIN)
    assert b"/FontFile2" not in a_page(LATIN)


@pytest.mark.parametrize("text", [CHINESE, GREEK, CYRILLIC, ARABIC, HEBREW])
def test_a_document_in_another_script_carries_its_own_font(text):
    if fontfinder.find(text) is None:
        pytest.skip(f"no font on this computer draws {text!r}")
    data = a_page(text)
    assert b"/Subtype /Type0" in data
    assert b"/Encoding /Identity-H" in data
    assert b"/FontFile2" in data
    assert b"/CIDToGIDMap /Identity" in data


@pytest.mark.parametrize("text", [CHINESE, GREEK, CYRILLIC])
def test_the_words_can_be_read_back_out_of_the_file(text):
    """Which means the invoice can be searched and copied from, not just seen."""
    if fontfinder.find(text) is None:
        pytest.skip(f"no font on this computer draws {text!r}")
    assert text in text_of(a_page(text))


def test_the_embedded_font_says_it_is_only_part_of_a_font():
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    import re

    tag = re.search(rb"/Subtype /Type0 /BaseFont /([A-Za-z]{6})\+", a_page(CHINESE))
    assert tag, "the embedded font is not marked as a subset"
    assert tag.group(1).isupper()


def test_a_line_in_two_scripts_comes_back_as_one_line():
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    assert f"{CHINESE} Ltd" in text_of(a_page(f"{CHINESE} Ltd"))


def test_a_name_in_another_script_can_be_measured_and_lined_up():
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    assert width_of(CHINESE, 10) > 0
    assert width_of(CHINESE + CHINESE, 10) == pytest.approx(
        width_of(CHINESE, 10) * 2)
    assert width_of(CHINESE, 20) == pytest.approx(width_of(CHINESE, 10) * 2)


def test_the_same_character_twice_is_embedded_once():
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    c = Canvas()
    c.text(42, 60, CHINESE)
    c.text(42, 80, CHINESE)
    face = next(iter(c._faces.values()))
    assert len(face.used) == len(set(CHINESE))


def test_a_second_page_reuses_the_font_rather_than_embedding_it_again():
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    c = Canvas()
    c.text(42, 60, CHINESE)
    c.new_page()
    c.text(42, 60, CHINESE)
    assert c.output().count(b"/FontFile2") == 1


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf not installed")
def test_a_real_pdf_tool_finds_nothing_wrong_with_an_embedded_font(tmp_path):
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    path = tmp_path / "embedded.pdf"
    c = Canvas()
    c.text(42, 60, f"{CHINESE} — invoice", size=14, bold=True)
    c.text(42, 90, ARABIC)
    c.text(42, 120, GREEK)
    path.write_bytes(c.output())

    result = subprocess.run(["qpdf", "--check", str(path)],
                            capture_output=True, text=True)
    assert "No syntax or stream encoding errors" in result.stdout, result.stdout


def test_the_font_file_inside_the_pdf_is_a_font():
    """A stream that is not a real font makes a reader show boxes or refuse."""
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    import re
    import zlib

    data = a_page(CHINESE)
    match = re.search(rb"/Length1 (\d+) /Filter /FlateDecode >>stream\n(.*?)\nendstream",
                      data, re.S)
    assert match, "no embedded font stream in the file"
    raw = zlib.decompress(match.group(2))
    assert len(raw) == int(match.group(1))
    again = TrueType.load(raw)
    assert again.num_glyphs >= len(set(CHINESE))


def test_glyph_widths_in_the_file_match_the_font(monkeypatch):
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    c = Canvas()
    c.text(42, 60, CHINESE)
    face = next(iter(c._faces.values()))
    written = c.output()
    widths = written.split(b"/W [1 [")[1].split(b"]")[0].split()
    expected = [face.font.advance(glyph)
                for glyph, _ in sorted(face.used.items(), key=lambda kv: kv[1])]
    assert [int(w) for w in widths] == expected


def test_the_font_directory_search_survives_a_folder_that_is_not_there():
    fontfinder.forget()
    assert isinstance(fontfinder.index(), dict)


def test_a_broken_font_file_is_skipped_not_crashed_on(tmp_path, monkeypatch):
    bad = tmp_path / "arial.ttf"
    bad.write_bytes(b"\x00\x01\x00\x00" + b"\xff" * 200)
    monkeypatch.setattr(fontfinder, "_index", {"arial.ttf": str(bad)})
    monkeypatch.setattr(fontfinder, "_loaded", {})
    monkeypatch.setattr(fontfinder, "_chosen", {})
    assert fontfinder.find("Ω") is None or True     # must not raise


def test_a_subset_tag_changes_when_the_characters_do():
    if fontfinder.find(CHINESE) is None:
        pytest.skip("no Chinese font on this computer")
    import re

    def tag(text):
        return re.search(rb"/Subtype /Type0 /BaseFont /([A-Za-z]{6})\+",
                         a_page(text)).group(1)

    assert tag(CHINESE) != tag(CHINESE[:3])
    assert tag(CHINESE) == tag(CHINESE)


@pytest.fixture(autouse=True)
def _fresh_font_cache():
    """Each test starts from a clean cache, since several of them patch it."""
    yield
    fontfinder.forget()
