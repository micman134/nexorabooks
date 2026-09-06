"""Reading a TrueType font, and cutting a small one out of it.

Nexora Books draws its PDFs with the fonts built into the PDF format, which
between them cover Western Europe and nothing else. That is fine until somebody
in Cairo, Shanghai, Athens, Mumbai or Bangkok puts their own customer's name on
an invoice — and then their customer's name prints as a row of boxes on their
own paperwork, which is not a thing you can sell.

The fix is to embed a real font. This module does the reading and the cutting;
``app/fonts.py`` decides which font to use and ``app/pdfwriter.py`` puts it in
the file.

**Only the glyphs actually used are embedded.** A Chinese font is twenty
megabytes; an invoice uses perhaps sixty characters of it. Subsetting turns
that into a few kilobytes, which is the difference between a PDF that can be
emailed and one that cannot.

Two things here are deliberate and easy to undo by accident:

  * **Composite glyphs drag their components with them.** The letter "é" is
    often stored as a reference to "e" plus a reference to an accent. Subset
    the é without its components and it prints as nothing at all — worse than
    a box, because it looks like the text is simply missing. Because the kept
    glyphs are renumbered, a composite's references to its components have to
    be renumbered inside the glyph outline too.

  * **Embedding permission is checked.** A font's OS/2 table says whether its
    licence allows it to be embedded. Fonts marked as not embeddable are
    refused rather than used, because shipping somebody else's font inside a
    document you send to a customer is exactly what that bit is there to stop.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

#: Tables an embedded CIDFontType2 needs, plus the hinting ones worth keeping.
KEEP = ("head", "hhea", "maxp", "hmtx", "glyf", "loca", "cvt ", "fpgm", "prep")


class FontError(Exception):
    """This file is not a font this module can use."""


@dataclass
class Subset:
    """A cut-down font, and where each of its glyphs came from.

    Glyphs are renumbered so the subset holds only what is used. A Chinese
    font has forty thousand glyphs: keeping the original numbering would mean
    keeping forty thousand entries in the offset and width tables even when
    the invoice uses fifteen characters, which is a couple of hundred kilobytes
    on every document for no reason at all.
    """

    data: bytes
    #: Original glyph id -> glyph id inside :attr:`data`.
    mapping: dict[int, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.mapping)


def _u16(data: bytes, at: int) -> int:
    return struct.unpack_from(">H", data, at)[0]


def _s16(data: bytes, at: int) -> int:
    return struct.unpack_from(">h", data, at)[0]


def _u32(data: bytes, at: int) -> int:
    return struct.unpack_from(">I", data, at)[0]


@dataclass
class TrueType:
    """One font file, parsed just enough to measure and subset it."""

    raw: bytes
    tables: dict[str, tuple[int, int]] = field(default_factory=dict)
    units_per_em: int = 1000
    num_glyphs: int = 0
    long_loca: bool = False
    #: Unicode code point -> glyph id, built lazily.
    _cmap: dict[int, int] | None = None
    _loca: list[int] | None = None
    _widths: list[int] | None = None
    _glyf: bytes | None = None
    name: str = ""
    ascent: int = 0
    descent: int = 0
    cap_height: int = 0
    italic_angle: float = 0.0
    bbox: tuple[int, int, int, int] = (0, 0, 1000, 1000)
    fs_type: int = 0
    is_bold: bool = False
    is_italic: bool = False

    # -- opening ----------------------------------------------------------
    @classmethod
    def load(cls, raw: bytes, index: int = 0) -> "TrueType":
        if len(raw) < 12:
            raise FontError("That file is too small to be a font.")

        if raw[:4] == b"ttcf":
            # A collection: several fonts in one file. Take the one asked for.
            count = _u32(raw, 8)
            if index >= count:
                raise FontError("That font collection has no such face.")
            offset = _u32(raw, 12 + index * 4)
        else:
            offset = 0

        tag = raw[offset:offset + 4]
        if tag not in (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO"):
            raise FontError("That file is not a TrueType font.")
        if tag == b"OTTO":
            # PostScript outlines. Embeddable as CIDFontType0, which is a
            # different job; refused here rather than half-supported.
            raise FontError("OpenType/CFF fonts are not supported.")

        font = cls(raw=raw)
        count = _u16(raw, offset + 4)
        at = offset + 12
        for _ in range(count):
            name = raw[at:at + 4].decode("latin-1")
            start, length = _u32(raw, at + 8), _u32(raw, at + 12)
            font.tables[name] = (start, length)
            at += 16

        font._read_head()
        font._read_maxp()
        font._read_metrics()
        font._read_names()
        return font

    def _table(self, name: str) -> bytes:
        found = self.tables.get(name)
        if found is None:
            return b""
        start, length = found
        return self.raw[start:start + length]

    def _read_head(self) -> None:
        head = self._table("head")
        if len(head) < 54:
            raise FontError("That font has no usable header.")
        self.units_per_em = _u16(head, 18) or 1000
        self.bbox = (_s16(head, 36), _s16(head, 38), _s16(head, 40), _s16(head, 42))
        self.is_bold = bool(_u16(head, 44) & 1)
        self.is_italic = bool(_u16(head, 44) & 2)
        self.long_loca = _s16(head, 50) == 1

    def _read_maxp(self) -> None:
        maxp = self._table("maxp")
        if len(maxp) < 6:
            raise FontError("That font does not say how many glyphs it has.")
        self.num_glyphs = _u16(maxp, 4)

    def _read_metrics(self) -> None:
        hhea = self._table("hhea")
        if len(hhea) >= 36:
            self.ascent, self.descent = _s16(hhea, 4), _s16(hhea, 6)
        os2 = self._table("OS/2")
        if len(os2) >= 10:
            self.fs_type = _u16(os2, 8)
        if len(os2) >= 90:
            self.cap_height = _s16(os2, 88)
        if not self.cap_height:
            self.cap_height = int(self.ascent * 0.7)
        post = self._table("post")
        if len(post) >= 8:
            whole, frac = _s16(post, 4), _u16(post, 6)
            self.italic_angle = whole + frac / 65536.0

    def _read_names(self) -> None:
        """The PostScript name, which is what goes into the PDF."""
        table = self._table("name")
        if len(table) < 6:
            self.name = "EmbeddedFont"
            return
        count, storage = _u16(table, 2), _u16(table, 4)
        best = ""
        for i in range(count):
            at = 6 + i * 12
            if at + 12 > len(table):
                break
            platform = _u16(table, at)
            name_id = _u16(table, at + 6)
            length, offset = _u16(table, at + 8), _u16(table, at + 10)
            if name_id != 6:                       # 6 is the PostScript name
                continue
            raw = table[storage + offset:storage + offset + length]
            try:
                text = raw.decode("utf-16-be" if platform in (0, 3) else "latin-1")
            except UnicodeDecodeError:
                continue
            text = "".join(ch for ch in text if ch.isprintable() and ch not in " ()<>[]{}/%")
            if text:
                best = text
                break
        self.name = best or "EmbeddedFont"

    # -- what it can draw --------------------------------------------------
    @property
    def can_be_embedded(self) -> bool:
        """Whether this font's own licence allows it inside a document.

        Bit 1 set on its own means "no embedding at all". Bit 9 means the
        licence forbids anything but a preview. Either way, we do not use it.
        """
        restricted = self.fs_type & 0x000F
        bitmap_only = bool(self.fs_type & 0x0200)
        return restricted != 2 and not bitmap_only

    def cmap(self) -> dict[int, int]:
        if self._cmap is None:
            self._cmap = self._read_cmap()
        return self._cmap

    def _read_cmap(self) -> dict[int, int]:
        table = self._table("cmap")
        if len(table) < 4:
            return {}
        count = _u16(table, 2)
        best = None
        best_score = -1
        for i in range(count):
            at = 4 + i * 8
            if at + 8 > len(table):
                break
            platform, encoding = _u16(table, at), _u16(table, at + 2)
            offset = _u32(table, at + 4)
            # Prefer a full Unicode table (format 12) over the 16-bit one.
            score = {
                (3, 10): 5, (0, 4): 5, (0, 6): 5,
                (3, 1): 4, (0, 3): 4, (0, 2): 3, (0, 1): 3, (0, 0): 2,
            }.get((platform, encoding), 0)
            if score > best_score and offset < len(table):
                best, best_score = offset, score
        if best is None:
            return {}

        fmt = _u16(table, best)
        if fmt == 4:
            return self._cmap4(table, best)
        if fmt == 12:
            return self._cmap12(table, best)
        if fmt == 6:
            return self._cmap6(table, best)
        return {}

    @staticmethod
    def _cmap4(table: bytes, at: int) -> dict[int, int]:
        seg_x2 = _u16(table, at + 6)
        segments = seg_x2 // 2
        ends = at + 14
        starts = ends + seg_x2 + 2
        deltas = starts + seg_x2
        ranges = deltas + seg_x2

        out: dict[int, int] = {}
        for i in range(segments):
            end = _u16(table, ends + i * 2)
            start = _u16(table, starts + i * 2)
            delta = _u16(table, deltas + i * 2)
            range_offset = _u16(table, ranges + i * 2)
            if start > end:
                continue
            for code in range(start, min(end, 0xFFFF) + 1):
                if range_offset == 0:
                    glyph = (code + delta) & 0xFFFF
                else:
                    index = ranges + i * 2 + range_offset + (code - start) * 2
                    if index + 2 > len(table):
                        continue
                    glyph = _u16(table, index)
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
                if glyph:
                    out[code] = glyph
        return out

    @staticmethod
    def _cmap6(table: bytes, at: int) -> dict[int, int]:
        first, count = _u16(table, at + 6), _u16(table, at + 8)
        return {first + i: _u16(table, at + 10 + i * 2) for i in range(count)}

    @staticmethod
    def _cmap12(table: bytes, at: int) -> dict[int, int]:
        groups = _u32(table, at + 12)
        out: dict[int, int] = {}
        for i in range(groups):
            base = at + 16 + i * 12
            if base + 12 > len(table):
                break
            start, end, glyph = _u32(table, base), _u32(table, base + 4), _u32(table, base + 8)
            if end - start > 0x10FFFF:
                continue
            for offset in range(end - start + 1):
                out[start + offset] = glyph + offset
        return out

    def glyph_for(self, char: str) -> int:
        return self.cmap().get(ord(char), 0)

    def covers(self, text: str) -> bool:
        table = self.cmap()
        return all(ord(ch) in table for ch in text if ch not in "\r\n\t")

    def missing(self, text: str) -> set[str]:
        table = self.cmap()
        return {ch for ch in text if ch not in "\r\n\t" and ord(ch) not in table}

    # -- measuring ---------------------------------------------------------
    def widths(self) -> list[int]:
        """Advance width per glyph, in font units."""
        if self._widths is not None:
            return self._widths
        hhea, hmtx = self._table("hhea"), self._table("hmtx")
        long_count = _u16(hhea, 34) if len(hhea) >= 36 else 0
        out: list[int] = []
        last = self.units_per_em // 2
        for glyph in range(self.num_glyphs):
            if glyph < long_count and (glyph * 4 + 2) <= len(hmtx):
                last = _u16(hmtx, glyph * 4)
            out.append(last)
        self._widths = out
        return out

    def advance(self, glyph: int) -> int:
        """Width of one glyph in 1/1000 em, which is what PDF wants."""
        table = self.widths()
        if not table or glyph >= len(table):
            return 500
        return int(round(table[glyph] * 1000 / self.units_per_em))

    def text_width(self, text: str, size: float) -> float:
        table = self.cmap()
        total = 0
        for ch in text:
            total += self.advance(table.get(ord(ch), 0))
        return total * size / 1000.0

    # -- cutting a small font out of it ------------------------------------
    def loca(self) -> list[int]:
        if self._loca is not None:
            return self._loca
        raw = self._table("loca")
        count = self.num_glyphs + 1
        if self.long_loca:
            out = [_u32(raw, i * 4) for i in range(min(count, len(raw) // 4))]
        else:
            out = [_u16(raw, i * 2) * 2 for i in range(min(count, len(raw) // 2))]
        self._loca = out
        return out

    def _glyph_bytes(self, glyph: int) -> bytes:
        table = self.loca()
        if glyph + 1 >= len(table):
            return b""
        start, end = table[glyph], table[glyph + 1]
        if end <= start:
            return b""                       # an empty glyph, such as a space
        if self._glyf is None:
            # Cached: a CJK font's outlines run to sixteen megabytes and
            # slicing that out of the file once per glyph is not free.
            self._glyf = self._table("glyf")
        return self._glyf[start:end]

    @staticmethod
    def _component_refs(data: bytes) -> list[tuple[int, int]]:
        """(position, glyph id) for each component of a composite glyph.

        The position is where the id sits inside ``data``, so a subset can
        renumber it in place. A simple glyph gives an empty list.
        """
        if len(data) < 10 or _s16(data, 0) >= 0:
            return []
        out: list[tuple[int, int]] = []
        at = 10
        while at + 4 <= len(data):
            flags = _u16(data, at)
            out.append((at + 2, _u16(data, at + 2)))
            at += 4
            at += 4 if flags & 0x0001 else 2          # ARG_1_AND_2_ARE_WORDS
            if flags & 0x0008:
                at += 2                                # WE_HAVE_A_SCALE
            elif flags & 0x0040:
                at += 4                                # X_AND_Y_SCALE
            elif flags & 0x0080:
                at += 8                                # TWO_BY_TWO
            if not flags & 0x0020:                     # MORE_COMPONENTS
                break
        return out

    def _components(self, glyph: int) -> list[int]:
        """The glyphs a composite is built from. Empty for a simple glyph."""
        return [index for _, index in
                self._component_refs(self._glyph_bytes(glyph))]

    def closure(self, glyphs: set[int]) -> set[int]:
        """Every glyph needed, including the pieces composites are made of."""
        needed = {0} | {g for g in glyphs if 0 <= g < self.num_glyphs}
        stack = list(needed)
        while stack:
            for component in self._components(stack.pop()):
                if component not in needed and component < self.num_glyphs:
                    needed.add(component)
                    stack.append(component)
        return needed

    def subset(self, glyphs: set[int],
               assigned: dict[int, int] | None = None) -> Subset:
        """A valid font containing only the glyphs asked for, renumbered.

        Glyph 0 — the box shown for anything missing — is always kept, and
        always first, because a font whose glyph 0 is something else is a font
        that draws the wrong thing when it fails.

        ``assigned`` fixes the new id of a glyph. The PDF writer hands out ids
        as it prints, long before the font is cut, so those ids have to be
        honoured here; the components a composite glyph needs are added after
        whatever has already been claimed.
        """
        needed = self.closure(glyphs)
        mapping: dict[int, int] = {0: 0}
        for old, new in (assigned or {}).items():
            if old in needed and new > 0:
                mapping[old] = new
        spare = max(mapping.values()) + 1
        for old in sorted(needed):
            if old not in mapping:
                mapping[old] = spare
                spare += 1

        count = max(mapping.values()) + 1
        order: list[int | None] = [None] * count
        for old, new in mapping.items():
            order[new] = old

        glyf_parts: list[bytes] = []
        offsets: list[int] = [0]
        at = 0
        for old in order:
            data = self._glyph_bytes(old) if old is not None else b""
            refs = self._component_refs(data)
            if refs:
                data = bytearray(data)
                for position, index in refs:
                    struct.pack_into(">H", data, position, mapping.get(index, 0))
                data = bytes(data)
            if len(data) % 4:                          # each glyph is 4-aligned
                data += b"\x00" * (4 - len(data) % 4)
            glyf_parts.append(data)
            at += len(data)
            offsets.append(at)
        glyf = b"".join(glyf_parts)

        long_loca = at > 0x1FFFF
        if long_loca:
            loca = b"".join(struct.pack(">I", o) for o in offsets)
        else:
            loca = b"".join(struct.pack(">H", o // 2) for o in offsets)

        widths = self.widths()
        hmtx = b"".join(
            struct.pack(">Hh",
                        widths[old] if old is not None and old < len(widths) else 0,
                        0)
            for old in order
        )

        head = bytearray(self._table("head"))
        struct.pack_into(">h", head, 50, 1 if long_loca else 0)
        struct.pack_into(">I", head, 8, 0)             # checkSumAdjustment
        hhea = bytearray(self._table("hhea"))
        if len(hhea) >= 36:
            struct.pack_into(">H", hhea, 34, count)    # numberOfHMetrics
        maxp = bytearray(self._table("maxp"))
        if len(maxp) >= 6:
            struct.pack_into(">H", maxp, 4, count)

        built = {
            "head": bytes(head), "hhea": bytes(hhea), "maxp": bytes(maxp),
            "hmtx": hmtx, "loca": loca, "glyf": glyf,
        }
        for optional in ("cvt ", "fpgm", "prep"):
            data = self._table(optional)
            if data:
                built[optional] = data

        return Subset(data=_assemble(built), mapping=mapping)


def _pad(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 4)


def _checksum(data: bytes) -> int:
    data = _pad(data)
    total = 0
    for i in range(0, len(data), 4):
        total = (total + _u32(data, i)) & 0xFFFFFFFF
    return total


def _assemble(tables: dict[str, bytes]) -> bytes:
    """Write a font file out of a set of tables."""
    names = sorted(tables)
    count = len(names)
    search = 1
    entry = 0
    while search * 2 <= count:
        search *= 2
        entry += 1

    header = struct.pack(">IHHHH", 0x00010000, count, search * 16, entry,
                         count * 16 - search * 16)
    directory = bytearray()
    body = bytearray()
    offset = 12 + count * 16
    for name in names:
        data = tables[name]
        directory += struct.pack(">4sIII", name.encode("latin-1"),
                                 _checksum(data), offset + len(body), len(data))
        body += _pad(data)
    return bytes(header) + bytes(directory) + bytes(body)
