"""A small PDF writer, in plain Python, with nothing to install.

Customers expect a PDF invoice attached to an email. Every library that makes
one is a compiled dependency, and a compiled dependency is a Windows build that
can fail on somebody else's machine in a way I cannot reproduce or fix for them.
So this writes the PDF itself.

It can afford to, because PDF is a text format and an invoice needs very
little of it: some lines of text, some rules, a logo. The one thing that
usually forces a library — embedding a font — is avoided entirely by using the
fourteen typefaces every PDF reader is required to have built in. Nothing is
bundled, nothing is compiled, and the output opens in Acrobat, Preview, Chrome
and every phone.

The base fonts cover Latin text and no more, so anything outside that — a
customer's name in Chinese, Japanese, Korean, Greek, Cyrillic, Hebrew or
Arabic — is printed with a font found on the computer the invoice is made on
and embedded into the document, cut down to only the characters used (see
``app/fonts.py``, ``app/ttf.py`` and ``app/shaping.py``). An invoice in Latin
script never touches a font file and comes out exactly as it did before. A
currency symbol that neither the base fonts nor any font here can draw prints
as its three-letter code, which is what an international invoice does anyway —
"NGN 1,250,000.00" reads correctly to everybody.

What is **not** done: the Indic scripts and Thai are printed character by
character, so their characters all appear but the ligatures and the mark
placement a full text engine would apply do not. That is a known limitation
rather than an oversight; documents in those scripts still read, and printing
from the browser gives typographically correct output in the meantime.

Coordinates here run from the **top** left and downwards, because that is how a
person lays out a page. PDF's own upside-down coordinates are dealt with on the
way out and never appear in calling code.
"""
from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass, field

from . import fonts as fontfinder
from .shaping import is_rtl, shape

# --------------------------------------------------------------------------
# Paper
# --------------------------------------------------------------------------

A4 = (595.28, 841.89)          # points, at 72 to the inch
LETTER = (612.0, 792.0)

HELVETICA = "Helvetica"
HELVETICA_BOLD = "Helvetica-Bold"
HELVETICA_OBLIQUE = "Helvetica-Oblique"

#: Character widths for the two faces used, in thousandths of the font size.
#: These are Adobe's own metrics for the built-in typefaces; without them a
#: right-aligned column of figures would not line up.
_HELV = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
)
_HELV_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 "
    "975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 "
    "333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
    "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584"
)


def _widths(table: str) -> dict[int, int]:
    return {32 + i: int(w) for i, w in enumerate(table.split())}


WIDTHS = {
    HELVETICA: _widths(_HELV),
    HELVETICA_BOLD: _widths(_HELV_BOLD),
    HELVETICA_OBLIQUE: _widths(_HELV),
}

#: What a character that the built-in fonts cannot show becomes instead.
FOLD = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
    "•": "-", "‹": "<", "›": ">", "−": "-",
}


def encodable(text: str) -> bool:
    """True when every character can actually be printed by a built-in font."""
    try:
        clean(text).encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def clean(text) -> str:
    """Fold the typography a PDF base font cannot show into what it can."""
    out = []
    for ch in str(text or ""):
        out.append(FOLD.get(ch, ch))
    return "".join(out)


def _pdf_text(text: str) -> bytes:
    """Escape a string for a PDF literal, dropping what cannot be shown."""
    encoded = clean(text).encode("cp1252", errors="replace")
    return (encoded.replace(b"\\", b"\\\\")
                   .replace(b"(", b"\\(")
                   .replace(b")", b"\\)"))


def _base_width(text: str, size: float, font: str) -> float:
    table = WIDTHS.get(font, WIDTHS[HELVETICA])
    total = 0
    for ch in text.encode("cp1252", errors="replace").decode("cp1252"):
        total += table.get(ord(ch), 556)
    return total * size / 1000.0


def plan(text, font: str = HELVETICA) -> list[tuple[object, str]]:
    """Split a string into the pieces to print, each with the font to use.

    A piece whose font is ``None`` is printed with the built-in typefaces,
    which is every piece of every Latin document. Right-to-left text is put in
    printing order here, once, so measuring and drawing cannot disagree.
    """
    value = clean(text)
    if not value or not fontfinder.needs_embedding(value):
        return [(None, value)]

    bold = font == HELVETICA_BOLD
    if is_rtl(value):
        picked = fontfinder.find(value, bold)
        value = shape(value, picked.font.covers if picked else (lambda ch: False))
    return fontfinder.plan(value, bold)


def printable(text) -> bool:
    """True when this will actually appear on the page.

    Either the built-in typefaces have the characters, or a font on this
    computer does and can be embedded. A currency symbol that fails both is
    printed as its three-letter code instead of as a question mark.
    """
    for face, piece in plan(str(text or "")):
        if face is None and fontfinder.needs_embedding(piece):
            return False
    return True


def width_of(text: str, size: float, font: str = HELVETICA) -> float:
    """How wide this string will print, in points."""
    total = 0.0
    for face, piece in plan(text, font):
        if face is None:
            total += _base_width(piece, size, font)
        else:
            total += face.font.text_width(piece, size)
    return total


def _split_word(word: str, size: float, limit: float, font: str) -> list[str]:
    """Break a word that cannot fit on a line of its own.

    Needed for more than the odd long word: Chinese and Japanese are written
    without spaces, so a whole description is one "word" and would otherwise
    run straight off the edge of the page.
    """
    out: list[str] = []
    piece = ""
    for char in word:
        if piece and width_of(piece + char, size, font) > limit:
            out.append(piece)
            piece = char
        else:
            piece += char
    if piece:
        out.append(piece)
    return out


def wrap(text: str, size: float, limit: float, font: str = HELVETICA) -> list[str]:
    """Break text into lines that fit inside ``limit`` points."""
    lines: list[str] = []
    for paragraph in str(text or "").split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            trial = f"{line} {word}" if line else word
            if width_of(trial, size, font) <= limit:
                line = trial
                continue
            if line:
                lines.append(line)
            if width_of(word, size, font) <= limit:
                line = word
                continue
            pieces = _split_word(word, size, limit, font)
            lines.extend(pieces[:-1])
            line = pieces[-1] if pieces else ""
        if line:
            lines.append(line)
    return lines


def truncate(text: str, size: float, limit: float, font: str = HELVETICA) -> str:
    """Shorten with an ellipsis so a long name cannot run into the next column."""
    if width_of(text, size, font) <= limit:
        return text
    trimmed = str(text or "")
    while trimmed and width_of(trimmed + "...", size, font) > limit:
        trimmed = trimmed[:-1]
    return trimmed + "..."


# --------------------------------------------------------------------------
# Embedding a font
# --------------------------------------------------------------------------


def _subset_tag(face) -> str:
    """The six-letter prefix a subset font's name must carry.

    Two documents that embed different cuts of the same font must not claim to
    hold the same font, or a reader that has cached one will draw the other
    with it. The tag is derived from what was actually included, so it is the
    same every time for the same content and different the moment that changes.
    """
    seed = (face.font.name + "|" + ",".join(str(g) for g in sorted(face.used))).encode()
    digest = hashlib.sha256(seed).digest()
    return "".join(chr(ord("A") + b % 26) for b in digest[:6])


def _to_unicode(face) -> bytes:
    """The table that lets a reader copy the text back out as characters.

    Without it an Arabic or Chinese invoice looks right and cannot be searched,
    copied or read by anything automated — which for an invoice is a real
    defect, not a nicety.
    """
    entries = [f"<{cid:04X}> <{text.encode('utf-16-be').hex().upper()}>"
               for cid, text in sorted(face.text_of.items())]

    body = ["/CIDInit /ProcSet findresource begin",
            "12 dict begin begincmap",
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
            "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
            "1 begincodespacerange <0000> <FFFF> endcodespacerange"]
    for at in range(0, len(entries), 100):
        chunk = entries[at:at + 100]
        body.append(f"{len(chunk)} beginbfchar")
        body.extend(chunk)
        body.append("endbfchar")
    body += ["endcmap CMapName currentdict /CMap defineresource pop",
             "end", "end"]
    return "\n".join(body).encode("ascii")


def _embed(face, add) -> int:
    """Write one embedded font into the file and return its object number."""
    font = face.font
    subset = font.subset(set(face.used), assigned=face.used)
    scale = 1000.0 / (font.units_per_em or 1000)
    name = f"{_subset_tag(face)}+{font.name}"

    packed = zlib.compress(subset.data, 6)
    file_id = add(
        f"<< /Length {len(packed)} /Length1 {len(subset.data)} "
        f"/Filter /FlateDecode >>stream\n".encode() + packed + b"\nendstream"
    )

    flags = 4 | (64 if font.italic_angle else 0)      # 4: not a Latin text font
    left, bottom, right, top = (int(v * scale) for v in font.bbox)
    descriptor = add((
        f"<< /Type /FontDescriptor /FontName /{name} /Flags {flags} "
        f"/FontBBox [{left} {bottom} {right} {top}] "
        f"/ItalicAngle {font.italic_angle:.0f} /Ascent {int(font.ascent * scale)} "
        f"/Descent {int(font.descent * scale)} "
        f"/CapHeight {int(font.cap_height * scale) or 700} "
        f"/StemV {120 if font.is_bold else 80} /FontFile2 {file_id} 0 R >>"
    ).encode())

    highest = max(face.used.values(), default=0)
    by_cid = {cid: glyph for glyph, cid in face.used.items()}
    widths = " ".join(str(font.advance(by_cid[cid])) if cid in by_cid else "500"
                      for cid in range(1, highest + 1))
    descendant = add((
        f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{name} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
        f"/Supplement 0 >> /FontDescriptor {descriptor} 0 R /DW 1000 "
        f"/W [1 [{widths}]] /CIDToGIDMap /Identity >>"
    ).encode())

    table = _to_unicode(face)
    packed_table = zlib.compress(table, 6)
    unicode_id = add(
        f"<< /Length {len(packed_table)} /Filter /FlateDecode >>stream\n".encode()
        + packed_table + b"\nendstream"
    )
    return add((
        f"<< /Type /Font /Subtype /Type0 /BaseFont /{name} /Encoding /Identity-H "
        f"/DescendantFonts [{descendant} 0 R] /ToUnicode {unicode_id} 0 R >>"
    ).encode())


# --------------------------------------------------------------------------
# Pictures
# --------------------------------------------------------------------------


@dataclass
class Picture:
    width: int
    height: int
    data: bytes
    colourspace: str                 # DeviceRGB or DeviceGray
    filter_name: str                 # DCTDecode for JPEG, FlateDecode for the rest
    bits: int = 8


def read_jpeg(raw: bytes) -> Picture | None:
    """A JPEG goes into a PDF exactly as it is — the format is already there."""
    if not raw.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i < len(raw) - 9:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(raw[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(raw[i + 5:i + 7], "big")
            width = int.from_bytes(raw[i + 7:i + 9], "big")
            components = raw[i + 9]
            if components not in (1, 3):
                return None          # CMYK: rare, and not worth guessing at
            return Picture(width, height, raw,
                           "DeviceGray" if components == 1 else "DeviceRGB",
                           "DCTDecode")
        i += 2 + length
    return None


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> bytes:
    """Undo the per-row filters PNG applies before compressing."""
    stride = width * channels
    out = bytearray()
    previous = bytearray(stride)
    pos = 0
    for _ in range(height):
        if pos >= len(raw):
            break
        kind = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if len(line) < stride:
            line.extend(b"\x00" * (stride - len(line)))
        if kind == 1:                                     # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:                                   # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:                                   # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:                                   # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                nearest = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + nearest) & 0xFF
        out.extend(line)
        previous = line
    return bytes(out)


def read_png(raw: bytes) -> Picture | None:
    """Decode a PNG far enough to put it in a PDF.

    Transparency is composited onto white rather than carried through: an
    invoice is printed on white paper, so the result is what the customer sees
    anyway, and it avoids a soft mask for no gain.
    """
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    width = height = depth = colour = 0
    idat = bytearray()
    palette = b""
    trns = b""
    while pos + 8 <= len(raw):
        length = int.from_bytes(raw[pos:pos + 4], "big")
        kind = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            depth, colour = body[8], body[9]
            if body[12] != 0:
                return None                    # interlaced: rare for a logo
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.extend(body)
        elif kind == b"IEND":
            break

    if depth != 8 or not width or not height:
        return None                            # 16-bit and 1/2/4-bit are rare
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        return None

    try:
        pixels = _unfilter(zlib.decompress(bytes(idat)), width, height, channels)
    except zlib.error:
        return None

    if colour == 2:
        rgb, space = pixels, "DeviceRGB"
    elif colour == 0:
        rgb, space = pixels, "DeviceGray"
    elif colour == 3:
        out = bytearray()
        for value in pixels:
            base = value * 3
            out.extend(palette[base:base + 3] or b"\x00\x00\x00")
        rgb, space = bytes(out), "DeviceRGB"
    elif colour == 4:                          # grey plus alpha
        out = bytearray()
        for i in range(0, len(pixels), 2):
            grey, alpha = pixels[i], pixels[i + 1]
            out.append((grey * alpha + 255 * (255 - alpha)) // 255)
        rgb, space = bytes(out), "DeviceGray"
    else:                                      # RGBA
        out = bytearray()
        for i in range(0, len(pixels), 4):
            alpha = pixels[i + 3]
            for c in range(3):
                out.append((pixels[i + c] * alpha + 255 * (255 - alpha)) // 255)
        rgb, space = bytes(out), "DeviceRGB"

    return Picture(width, height, zlib.compress(rgb, 6), space, "FlateDecode")


def read_picture(raw: bytes) -> Picture | None:
    """Whatever this is, if it can go in a PDF."""
    if not raw:
        return None
    return read_png(raw) or read_jpeg(raw)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


@dataclass
class _Page:
    content: bytearray = field(default_factory=bytearray)
    pictures: dict[str, Picture] = field(default_factory=dict)
    #: Resource names of any embedded fonts this page prints with.
    faces: set = field(default_factory=set)


class Canvas:
    """Draw on pages with the origin at the top left, y increasing downwards."""

    def __init__(self, size=A4, margin: float = 42.0):
        self.width, self.height = size
        self.margin = margin
        self.pages: list[_Page] = [_Page()]
        self._current = 0
        self._picture_names: dict[bytes, str] = {}
        self._pictures: dict[str, Picture] = {}
        self._next_picture = 1
        #: Embedded fonts this document has used, by the font file they came
        #: from. Each carries the list of glyphs actually printed.
        self._faces: dict[int, fontfinder.Face] = {}

    # -- page handling ----------------------------------------------------

    @property
    def page(self) -> _Page:
        return self.pages[self._current]

    @property
    def right(self) -> float:
        return self.width - self.margin

    @property
    def usable(self) -> float:
        return self.width - 2 * self.margin

    def new_page(self) -> None:
        self.pages.append(_Page())
        self._current = len(self.pages) - 1

    def on_page(self, index: int):
        """Draw on an earlier page again — page numbers in the footer need it.

            with c.on_page(0):
                c.text(...)

        Leaves the canvas pointing wherever it was.
        """
        canvas = self

        class _Switch:
            def __enter__(self):
                self.previous = canvas._current
                canvas._current = max(0, min(index, len(canvas.pages) - 1))
                return canvas

            def __exit__(self, *exc):
                canvas._current = self.previous
                return False

        return _Switch()

    def _y(self, y: float) -> float:
        """Top-down to PDF's bottom-up."""
        return self.height - y

    def _write(self, text: str) -> None:
        self.page.content.extend(text.encode("ascii"))
        self.page.content.extend(b"\n")

    # -- drawing ----------------------------------------------------------

    def _face_for(self, loaded) -> fontfinder.Face:
        """The record of what this document has used out of one font file."""
        face = self._faces.get(id(loaded))
        if face is None:
            face = fontfinder.Face(f"FE{len(self._faces) + 1}", loaded)
            self._faces[id(loaded)] = face
        return face

    def text(self, x: float, y: float, value, *, size: float = 9.5,
             bold: bool = False, italic: bool = False, align: str = "left",
             colour: tuple[float, float, float] = (0, 0, 0),
             width: float | None = None) -> float:
        """One line of text. ``y`` is the baseline, measured from the top.

        ``align`` of "right" or "centre" measures the string and places it, so
        a column of figures lines up on its last digit.

        Anything the built-in typefaces cannot print — a name in Chinese,
        Greek, Cyrillic or Arabic — is drawn with a font found on this computer
        and embedded in the file. That is decided per piece of the line, so a
        reference number beside a Chinese company name still prints in the
        same typeface as the rest of the document.
        """
        font = HELVETICA_BOLD if bold else (HELVETICA_OBLIQUE if italic else HELVETICA)
        value = str(value if value is not None else "")
        if width is not None:
            value = truncate(value, size, width, font)

        pieces = plan(value, font)
        total = sum(_base_width(piece, size, font) if face is None
                    else face.font.text_width(piece, size)
                    for face, piece in pieces)
        if align == "right":
            x -= total
        elif align in ("centre", "center"):
            x -= total / 2

        # One text object for the whole line, however many fonts it takes, so
        # that anything reading the file back gets the line as a line.
        r, g, b = colour
        self._write("BT")
        at = x
        for loaded, piece in pieces:
            if not piece:
                continue
            if loaded is None:
                key = {HELVETICA: "F1", HELVETICA_BOLD: "F2",
                       HELVETICA_OBLIQUE: "F3"}[font]
                body = b"(" + _pdf_text(piece) + b") Tj"
                at += _base_width(piece, size, font)
            else:
                face = self._face_for(loaded)
                self.page.faces.add(face.key)
                key = face.key
                body = b"<" + face.encode(piece).hex().upper().encode() + b"> Tj"
                at += loaded.font.text_width(piece, size)
            self._write(f"/{key} {size:.2f} Tf {r:.3f} {g:.3f} {b:.3f} rg "
                        f"1 0 0 1 {x:.2f} {self._y(y):.2f} Tm")
            self.page.content.extend(body + b"\n")
            x = at
        self._write("ET")
        return total

    def paragraph(self, x: float, y: float, value, *, size: float = 9.5,
                  leading: float | None = None, limit: float | None = None,
                  bold: bool = False, colour=(0, 0, 0)) -> float:
        """Wrapped text. Returns the y just below the last line."""
        leading = leading or size * 1.35
        limit = limit or (self.right - x)
        font = HELVETICA_BOLD if bold else HELVETICA
        for line in wrap(str(value or ""), size, limit, font):
            self.text(x, y, line, size=size, bold=bold, colour=colour)
            y += leading
        return y

    def line(self, x1: float, y1: float, x2: float, y2: float, *,
             thickness: float = 0.5, colour=(0.78, 0.80, 0.82)) -> None:
        r, g, b = colour
        self._write(f"{r:.3f} {g:.3f} {b:.3f} RG {thickness:.2f} w "
                    f"{x1:.2f} {self._y(y1):.2f} m {x2:.2f} {self._y(y2):.2f} l S")

    def rule(self, y: float, *, thickness: float = 0.5, colour=(0.78, 0.80, 0.82)) -> None:
        self.line(self.margin, y, self.right, y, thickness=thickness, colour=colour)

    def rect(self, x: float, y: float, w: float, h: float, *,
             fill=None, stroke=None, thickness: float = 0.5) -> None:
        parts = []
        if fill:
            parts.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke:
            parts.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG "
                         f"{thickness:.2f} w")
        parts.append(f"{x:.2f} {self._y(y + h):.2f} {w:.2f} {h:.2f} re")
        parts.append("B" if (fill and stroke) else ("f" if fill else "S"))
        self._write(" ".join(parts))

    def picture(self, raw: bytes, x: float, y: float,
                max_width: float, max_height: float) -> tuple[float, float]:
        """Place an image, scaled to fit inside the box, keeping its shape.

        Returns the size actually used, or (0, 0) when the picture could not be
        read — a logo in a format this cannot decode is simply left out rather
        than made into an error the customer cannot act on.
        """
        picture = self._picture_named(raw)
        if picture is None:
            return 0.0, 0.0
        name, pic = picture
        scale = min(max_width / pic.width, max_height / pic.height)
        w, h = pic.width * scale, pic.height * scale
        self._write(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {self._y(y + h):.2f} cm "
                    f"/{name} Do Q")
        self.page.pictures[name] = pic
        return w, h

    def _picture_named(self, raw: bytes):
        key = raw[:64] + len(raw).to_bytes(8, "big")
        name = self._picture_names.get(key)
        if name is None:
            pic = read_picture(raw)
            if pic is None:
                return None
            name = f"Im{self._next_picture}"
            self._next_picture += 1
            self._picture_names[key] = name
            self._pictures[name] = pic
        else:
            pic = self._pictures[name]
        return name, pic

    # -- output -----------------------------------------------------------

    def output(self) -> bytes:
        """The finished file.

        Object numbers are handed out in order, except that the page tree is
        reserved up front: every page has to point at it, and it has to list
        every page, so one of the two must be written before it is known.
        """
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)              # object numbers start at 1

        def reserve() -> int:
            return add(b"")

        pages_id = reserve()

        font_ids = {}
        for key, base in (("F1", HELVETICA), ("F2", HELVETICA_BOLD),
                          ("F3", HELVETICA_OBLIQUE)):
            font_ids[key] = add(
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} "
                f"/Encoding /WinAnsiEncoding >>".encode()
            )

        embedded_ids: dict[str, int] = {}
        for face in self._faces.values():
            embedded_ids[face.key] = _embed(face, add)

        picture_ids: dict[str, int] = {}
        for name, pic in self._pictures.items():
            header = (
                f"<< /Type /XObject /Subtype /Image /Width {pic.width} "
                f"/Height {pic.height} /ColorSpace /{pic.colourspace} "
                f"/BitsPerComponent {pic.bits} /Filter /{pic.filter_name} "
                f"/Length {len(pic.data)} >>stream\n"
            ).encode()
            picture_ids[name] = add(header + pic.data + b"\nendstream")

        page_ids: list[int] = []
        for page in self.pages:
            body = zlib.compress(bytes(page.content), 6)
            stream = add(
                f"<< /Length {len(body)} /Filter /FlateDecode >>stream\n".encode()
                + body + b"\nendstream"
            )
            used = dict(font_ids)
            used.update({k: embedded_ids[k] for k in sorted(page.faces)})
            resources = ("<< /Font << " +
                         " ".join(f"/{k} {v} 0 R" for k, v in used.items()) +
                         " >>")
            if page.pictures:
                resources += (" /XObject << " + " ".join(
                    f"/{n} {picture_ids[n]} 0 R" for n in page.pictures) + " >>")
            resources += " >>"
            page_ids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox "
                f"[0 0 {self.width:.2f} {self.height:.2f}] /Resources {resources} "
                f"/Contents {stream} 0 R >>".encode()
            ))

        kids = " ".join(f"{i} 0 R" for i in page_ids)
        objects[pages_id - 1] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
        catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{number} 0 obj\n".encode())
            out.extend(body)
            out.extend(b"\nendobj\n")

        start = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
        out.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            out.extend(f"{offset:010d} 00000 n \n".encode())
        out.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n".encode()
        )
        return bytes(out)
