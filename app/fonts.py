"""Finding a font that can print the customer's own alphabet.

The PDFs are drawn with the fourteen typefaces every reader has built in, which
between them cover Western Europe. That is enough until somebody invoices a
customer called 上海东方贸易, Ελληνικά Ναυτιλιακά or ООО «Восток» — and then
their own paperwork prints their customer's name as a row of boxes.

This module answers one question: **which font on this computer can draw these
characters?** It does not ship a font. Two reasons, and they both matter:

  * A font that covers Chinese, Japanese, Korean, Arabic and Devanagari is
    tens of megabytes. Bundling one would multiply the size of the download
    for the benefit of a minority of customers, and nobody would thank us.
  * Every computer that needs those scripts already has fonts for them.
    Windows ships Microsoft YaHei, Meiryo, Malgun Gothic, Nirmala UI and
    Leelawadee; macOS and Linux have their own. Arial alone covers Latin,
    Greek, Cyrillic, Hebrew and Arabic.

So we look for the customer's own fonts, in a fixed order of preference, and
use the first one that can actually draw the text in front of us. If nothing
can, the text falls back to the built-in typefaces and prints as best it can —
which is what happens today, so nothing gets worse.

A font's own licence is respected: :attr:`app.ttf.TrueType.can_be_embedded`
reads the OS/2 permission bits and a font marked as not embeddable is skipped,
not used. That is the whole point of that bit.

**Customers can add their own.** Anything dropped into the ``fonts`` folder
inside the data directory is searched first, so a business whose script we have
not thought of can fix it themselves without waiting for a release.
"""
from __future__ import annotations

import os
import struct
import sys
import unicodedata
from dataclasses import dataclass

from .shaping import is_rtl
from .ttf import FontError, TrueType

struct_error = struct.error

#: Filenames tried in order, with the bold companion where there is one. The
#: order is deliberate: broad text fonts first, so ordinary European and
#: Cyrillic text uses a normal-looking face rather than the first CJK font
#: that happens to contain a Latin alphabet.
CANDIDATES: tuple[tuple[str, str, int], ...] = (
    # Windows: covers Latin, Greek, Cyrillic, Hebrew, Arabic, Vietnamese.
    ("arial.ttf", "arialbd.ttf", 0),
    ("segoeui.ttf", "segoeuib.ttf", 0),
    ("tahoma.ttf", "tahomabd.ttf", 0),
    # Widely installed on Linux, and a common download elsewhere.
    ("dejavusans.ttf", "dejavusans-bold.ttf", 0),
    ("freeserif.ttf", "freeserifbold.ttf", 0),
    ("freesans.ttf", "freesansbold.ttf", 0),
    # Indic and Thai on Windows.
    ("nirmala.ttf", "nirmalab.ttf", 0),
    ("leelawui.ttf", "leelauib.ttf", 0),
    ("leelawad.ttf", "leelawdb.ttf", 0),
    # Chinese, simplified then traditional.
    ("msyh.ttc", "msyhbd.ttc", 0),
    ("msyh.ttf", "msyhbd.ttf", 0),
    ("simsun.ttc", "", 0),
    ("simhei.ttf", "", 0),
    ("msjh.ttc", "msjhbd.ttc", 0),
    ("mingliu.ttc", "", 0),
    # Japanese.
    ("meiryo.ttc", "meiryob.ttc", 0),
    ("yugothm.ttc", "yugothb.ttc", 0),
    ("msgothic.ttc", "", 0),
    ("fonts-japanese-gothic.ttf", "", 0),
    ("ipag.ttf", "", 0),
    # Korean.
    ("malgun.ttf", "malgunbd.ttf", 0),
    ("gulim.ttc", "", 0),
    ("batang.ttc", "", 0),
    # Everything Chinese, Japanese and Korean, on Linux.
    ("wqy-zenhei.ttc", "", 0),
    ("wqy-microhei.ttc", "", 0),
    ("droidsansfallbackfull.ttf", "", 0),
    ("droidsansfallback.ttf", "", 0),
    # macOS.
    ("arial unicode.ttf", "", 0),
    ("arialuni.ttf", "", 0),
)

#: Folders searched for those files. Missing ones are simply skipped.
def _directories() -> list[str]:
    out: list[str] = []

    try:                                   # the customer's own drop-in folder
        from .config import data_dir
        out.append(str(data_dir() / "fonts"))
    except Exception:
        pass

    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        out.append(os.path.join(windir, "Fonts"))
        local = os.environ.get("LOCALAPPDATA")
        if local:                          # fonts installed for one user only
            out.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    elif sys.platform == "darwin":
        out += ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
                "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    else:
        out += ["/usr/share/fonts", "/usr/local/share/fonts",
                os.path.expanduser("~/.local/share/fonts"),
                os.path.expanduser("~/.fonts")]
    return out


_index: dict[str, str] | None = None
_loaded: dict[str, "Loaded | None"] = {}
_chosen: dict[tuple[str, bool], "Loaded | None"] = {}


def _build_index() -> dict[str, str]:
    """Every font file on this computer, by lowercased name.

    Walked once. A font folder holds a few hundred files, so this costs
    milliseconds, and it means a font in a subfolder — which is how Linux
    organises them — is found just the same.
    """
    found: dict[str, str] = {}
    for directory in _directories():
        if not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for name in files:
                low = name.lower()
                if low.endswith((".ttf", ".ttc")) and low not in found:
                    found[low] = os.path.join(root, name)
    return found


def index() -> dict[str, str]:
    global _index
    if _index is None:
        _index = _build_index()
    return _index


def forget() -> None:
    """Drop everything cached. Used by the tests, and after a font is added."""
    global _index
    _index = None
    _loaded.clear()
    _chosen.clear()


@dataclass(frozen=True, eq=False)
class Loaded:
    """A font file on this computer, parsed once and shared by every document."""

    path: str
    index: int
    bold: bool
    font: TrueType

    @property
    def id(self) -> str:
        return f"{self.path}#{self.index}"


class Face:
    """One font as used by one document, and what has been taken out of it.

    A document embeds only the glyphs it actually prints, so the record of
    which those are belongs to the document — not to the shared, cached font.
    """

    def __init__(self, key: str, loaded: Loaded):
        self.key = key                      # resource name in the PDF, "FE1"
        self.loaded = loaded
        self.font = loaded.font
        #: Original glyph id -> the id it will carry in the embedded subset,
        #: handed out in the order the characters are first printed.
        self.used: dict[int, int] = {}
        #: Subset glyph id -> the text it came from, for copy and paste.
        self.text_of: dict[int, str] = {}

    def cid(self, char: str) -> int:
        """The id to write into the PDF for this character, claiming it."""
        glyph = self.font.glyph_for(char)
        existing = self.used.get(glyph)
        if existing is not None:
            self.text_of.setdefault(existing, char)
            return existing
        assigned = len(self.used) + 1          # 0 stays .notdef
        self.used[glyph] = assigned
        self.text_of[assigned] = char
        return assigned

    def encode(self, text: str) -> bytes:
        """The two-byte-per-character string an Identity-H font is written in."""
        return b"".join(self.cid(ch).to_bytes(2, "big") for ch in text)

    def width(self, text: str, size: float) -> float:
        return self.font.text_width(text, size)


def _load(path: str, face_index: int, bold: bool) -> Loaded | None:
    key = f"{path}#{face_index}"
    if key in _loaded:
        return _loaded[key]
    found: Loaded | None = None
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        font = TrueType.load(raw, face_index)
        if font.can_be_embedded and font.cmap():
            found = Loaded(path=path, index=face_index, bold=bold, font=font)
    except (OSError, FontError, ValueError, IndexError, KeyError, struct_error):
        found = None                       # a broken font is not worth an error
    _loaded[key] = found
    return found


def _candidates(bold: bool):
    """Font files to try, in order, for this weight."""
    files = index()
    for regular, heavy, face_index in CANDIDATES:
        path = (files.get(heavy) if bold and heavy else None) or files.get(regular)
        if path:
            yield path, face_index, bool(bold and heavy and path == files.get(heavy))


def find(text: str, bold: bool = False) -> Loaded | None:
    """The first font on this computer that can draw all of ``text``.

    Returns ``None`` when nothing can, which is not an error — the caller
    prints what it can with the built-in typefaces instead.
    """
    wanted = "".join(sorted(set(ch for ch in text if not ch.isspace())))
    if not wanted:
        return None
    if (wanted, bold) in _chosen:
        return _chosen[(wanted, bold)]

    picked: Loaded | None = None
    for path, face_index, is_bold in _candidates(bold):
        loaded = _load(path, face_index, is_bold)
        if loaded is not None and loaded.font.covers(wanted):
            picked = loaded
            break
    _chosen[(wanted, bold)] = picked
    return picked


def _base_can_print(char: str) -> bool:
    """Whether a built-in PDF typeface has this character at all."""
    try:
        char.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def needs_embedding(text: str) -> bool:
    return any(not _base_can_print(ch) for ch in text)


def _has_marks(text: str) -> bool:
    """Whether anything here is a mark that sits on the letter before it."""
    return any(unicodedata.category(ch).startswith("M") for ch in text)


def plan(text: str, bold: bool = False) -> list[tuple[Loaded | None, str]]:
    """Split a string into runs, each with the font that should print it.

    ``None`` means the built-in typefaces. Latin text never reaches a font
    file at all, so an ordinary invoice costs nothing.

    Ordinarily only the characters the built-in fonts cannot print are handed
    to a found font, so "上海东方贸易 Ltd" keeps its "Ltd" in the same typeface
    as the rest of the document. Two cases take the whole line instead:

      * text with combining marks, where an accent separated from its letter
        would land beside it rather than on top of it;
      * right-to-left text, which has already been put into printing order as
        one piece and cannot be cut up again without undoing that.
    """
    if not text or not needs_embedding(text):
        return [(None, text)]

    if _has_marks(text) or is_rtl(text):
        whole = find(text, bold)
        if whole is not None:
            return [(whole, text)]

    # One font for everything the built-in typefaces cannot print, wherever
    # there is one that can manage the lot. Choosing per character would put
    # two different Chinese faces inside one company name, which looks broken
    # even when every character is right.
    single = find("".join(ch for ch in text if not _base_can_print(ch)), bold)

    runs: list[tuple[Loaded | None, str]] = []
    current: Loaded | None = None
    started = False
    buffer: list[str] = []
    for char in text:
        if _base_can_print(char):
            chosen = None
        else:
            chosen = single or find(char, bold)
        if not started:
            current, started = chosen, True
        elif chosen is not current:
            runs.append((current, "".join(buffer)))
            current, buffer = chosen, []
        buffer.append(char)
    runs.append((current, "".join(buffer)))
    return runs
