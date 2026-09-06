"""Getting the text back out of a PDF, with the position of every piece.

Banks hand customers a PDF because a PDF looks like the paper statement they
used to post. It is a terrible format to read figures out of — a PDF does not
contain a table, or rows, or columns. It contains instructions of the form
"put these glyphs at this point on the page", and the fact that they line up
into a table is a coincidence of arithmetic that only a human eye resolves.

So this does what the eye does. It reads every piece of text with the point it
was placed at, groups pieces that share a baseline into rows, works out where
the columns are from where the pieces start, and hands back a grid — which
``app/services/statements.py`` then reads exactly as though it had come out of
a spreadsheet.

Two things are worth being clear about.

**It only reads PDFs that contain text.** A scanned or photographed statement
contains a picture of a statement and no text at all. Nothing here can read
that, and it says so rather than returning an empty grid that looks like a
statement with no transactions in it.

**Nothing it reads is ever posted on its own.** The import screen shows every
line for a person to confirm before a single entry reaches the ledger, which
is what makes a best-effort reading of an awkward format acceptable at all. A
misreading is a visible wrong line on a screen, not a wrong figure in a set of
books.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field

#: Pieces closer than this vertically are on the same line. Two points is
#: about a quarter of a line at statement type sizes — enough to forgive the
#: half-point drift some generators put on a baseline, not enough to merge two
#: real rows.
LINE_TOLERANCE = 2.6

#: Column starts closer than this are the same column.
COLUMN_TOLERANCE = 12.0

#: A blank strip at least this wide, running the height of the table, is the
#: gap between two columns rather than a wide space inside one.
GUTTER = 5.0

#: Below this much recovered text, the file is a picture of a statement.
ENOUGH_TEXT = 40


class PdfError(Exception):
    """This PDF cannot be read as text."""


# --------------------------------------------------------------------------
# The object soup
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """A reference to another object: ``12 0 R``."""

    num: int
    gen: int = 0


@dataclass
class Stream:
    """A dictionary with a lump of bytes attached."""

    info: dict
    raw: bytes

    def data(self) -> bytes:
        return _decode_stream(self.info, self.raw)


_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"


def _skip(data: bytes, at: int) -> int:
    while at < len(data):
        ch = data[at:at + 1]
        if ch in b"%":                       # a comment runs to end of line
            while at < len(data) and data[at:at + 1] not in b"\r\n":
                at += 1
        elif ch in _WHITESPACE:
            at += 1
        else:
            break
    return at


def _name(data: bytes, at: int) -> tuple[str, int]:
    at += 1                                   # the slash
    start = at
    while at < len(data) and data[at:at + 1] not in _WHITESPACE + _DELIMITERS:
        at += 1
    raw = data[start:at]
    # #41 style escapes appear in names with odd characters in them.
    out = re.sub(rb"#([0-9A-Fa-f]{2})",
                 lambda m: bytes([int(m.group(1), 16)]), raw)
    return out.decode("latin-1"), at


def _literal_string(data: bytes, at: int) -> tuple[bytes, int]:
    at += 1
    depth, out = 1, bytearray()
    while at < len(data):
        ch = data[at]
        if ch == 0x5C:                        # backslash
            at += 1
            if at >= len(data):
                break
            nxt = data[at]
            simple = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
            if nxt in simple:
                out.append(simple[nxt]); at += 1
            elif 0x30 <= nxt <= 0x37:         # octal
                digits = bytearray()
                while at < len(data) and len(digits) < 3 and 0x30 <= data[at] <= 0x37:
                    digits.append(data[at]); at += 1
                out.append(int(digits, 8) & 0xFF)
            elif nxt in (10, 13):             # a line continuation
                at += 1
                if at < len(data) and data[at] == 10 and nxt == 13:
                    at += 1
            else:
                out.append(nxt); at += 1
        elif ch == 0x28:
            depth += 1; out.append(ch); at += 1
        elif ch == 0x29:
            depth -= 1
            at += 1
            if depth == 0:
                break
            out.append(ch)
        else:
            out.append(ch); at += 1
    return bytes(out), at


def _hex_string(data: bytes, at: int) -> tuple[bytes, int]:
    at += 1
    end = data.find(b">", at)
    if end < 0:
        end = len(data)
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", data[at:end])
    if len(digits) % 2:
        digits += b"0"
    return bytes.fromhex(digits.decode("ascii")), end + 1


_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_REFERENCE = re.compile(rb"\s*(\d+)\s+(\d+)\s+R\b")


def parse(data: bytes, at: int = 0):
    """One PDF object, starting at ``at``. Returns (value, position after)."""
    at = _skip(data, at)
    if at >= len(data):
        return None, at

    ch = data[at:at + 1]

    if ch == b"/":
        return _name(data, at)
    if ch == b"(":
        return _literal_string(data, at)
    if ch == b"<":
        if data[at:at + 2] == b"<<":
            return _dictionary(data, at)
        return _hex_string(data, at)
    if ch == b"[":
        at += 1
        items = []
        while True:
            at = _skip(data, at)
            if at >= len(data) or data[at:at + 1] == b"]":
                return items, at + 1
            value, at = parse(data, at)
            items.append(value)
    if data[at:at + 4] == b"true":
        return True, at + 4
    if data[at:at + 5] == b"false":
        return False, at + 5
    if data[at:at + 4] == b"null":
        return None, at + 4

    reference = _REFERENCE.match(data, at)
    if reference:
        return Ref(int(reference.group(1)), int(reference.group(2))), reference.end()

    number = _NUMBER.match(data, at)
    if number:
        text = number.group(0)
        value = float(text) if b"." in text else int(text)
        return value, number.end()

    # Something unrecognised — step over one token so parsing can continue.
    end = at
    while end < len(data) and data[end:end + 1] not in _WHITESPACE + _DELIMITERS:
        end += 1
    return None, max(end, at + 1)


def _dictionary(data: bytes, at: int):
    at += 2
    out: dict = {}
    while True:
        at = _skip(data, at)
        if at >= len(data):
            return out, at
        if data[at:at + 2] == b">>":
            at += 2
            break
        if data[at:at + 1] != b"/":
            _value, at = parse(data, at)      # a stray token; skip it
            continue
        key, at = _name(data, at)
        value, at = parse(data, at)
        out[key] = value

    # A dictionary followed by "stream" owns the bytes that come next.
    after = _skip(data, at)
    if data[after:after + 6] == b"stream":
        after += 6
        if data[after:after + 2] == b"\r\n":
            after += 2
        elif data[after:after + 1] in (b"\n", b"\r"):
            after += 1
        end = data.find(b"endstream", after)
        if end < 0:
            end = len(data)
        body = data[after:end]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        elif body.endswith(b"\n") or body.endswith(b"\r"):
            body = body[:-1]
        return Stream(out, body), end + 9
    return out, at


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def _undo_predictor(data: bytes, info: dict) -> bytes:
    """Undo the PNG-style row prediction some producers apply before Flate."""
    params = info.get("DecodeParms") or info.get("DP") or {}
    if isinstance(params, list):
        params = next((p for p in params if isinstance(p, dict)), {})
    if not isinstance(params, dict):
        return data
    predictor = params.get("Predictor", 1)
    if not isinstance(predictor, int) or predictor < 10:
        return data

    columns = int(params.get("Columns", 1) or 1)
    colours = int(params.get("Colors", 1) or 1)
    bits = int(params.get("BitsPerComponent", 8) or 8)
    stride = max(1, (columns * colours * bits + 7) // 8)
    step = max(1, colours * bits // 8)

    out = bytearray()
    previous = bytearray(stride)
    at = 0
    while at + 1 <= len(data):
        kind = data[at]
        at += 1
        row = bytearray(data[at:at + stride])
        at += stride
        if not row:
            break
        if len(row) < stride:
            row.extend(b"\x00" * (stride - len(row)))
        if kind == 1:
            for i in range(step, stride):
                row[i] = (row[i] + row[i - step]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                row[i] = (row[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = row[i - step] if i >= step else 0
                row[i] = (row[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = row[i - step] if i >= step else 0
                b = previous[i]
                c = previous[i - step] if i >= step else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[i] = (row[i] + (a if (pa <= pb and pa <= pc)
                                    else (b if pb <= pc else c))) & 0xFF
        out.extend(row)
        previous = row
    return bytes(out)


def _ascii85(data: bytes) -> bytes:
    import base64

    body = re.sub(rb"\s", b"", data)
    if body.startswith(b"<~"):
        body = body[2:]
    end = body.find(b"~>")
    if end >= 0:
        body = body[:end]
    try:
        return base64.a85decode(body)
    except Exception:
        return b""


def _decode_stream(info: dict, raw: bytes) -> bytes:
    filters = info.get("Filter") or info.get("F") or []
    if isinstance(filters, str):
        filters = [filters]
    if not isinstance(filters, list):
        filters = []

    data = raw
    for name in filters:
        if name in ("FlateDecode", "Fl"):
            try:
                data = zlib.decompress(data)
            except zlib.error:
                try:                          # some producers omit the trailer
                    data = zlib.decompressobj().decompress(data)
                except zlib.error:
                    return b""
            data = _undo_predictor(data, info)
        elif name in ("ASCIIHexDecode", "AHx"):
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
            if len(digits) % 2:
                digits += b"0"
            data = bytes.fromhex(digits.decode("ascii"))
        elif name in ("ASCII85Decode", "A85"):
            data = _ascii85(data)
        elif name in ("LZWDecode", "LZW"):
            data = _lzw(data)
            data = _undo_predictor(data, info)
        elif name in ("RunLengthDecode", "RL"):
            data = _runlength(data)
        else:
            # An image filter (DCT, JPX, CCITT). Not text; nothing to read.
            return b""
    return data


def _lzw(data: bytes) -> bytes:
    out = bytearray()
    table = {i: bytes([i]) for i in range(256)}
    nxt, width = 258, 9
    previous = b""
    buffer = value = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        value += 8
        while value >= width:
            value -= width
            code = (buffer >> value) & ((1 << width) - 1)
            if code == 256:
                table = {i: bytes([i]) for i in range(256)}
                nxt, width, previous = 258, 9, b""
                continue
            if code == 257:
                return bytes(out)
            if previous == b"":
                entry = table.get(code, b"")
            elif code in table:
                entry = table[code]
            else:
                entry = previous + previous[:1]
            out.extend(entry)
            if previous:
                table[nxt] = previous + entry[:1]
                nxt += 1
                if nxt + 1 >= (1 << width) and width < 12:
                    width += 1
            previous = entry
    return bytes(out)


def _runlength(data: bytes) -> bytes:
    out, at = bytearray(), 0
    while at < len(data):
        length = data[at]
        at += 1
        if length == 128:
            break
        if length < 128:
            out.extend(data[at:at + length + 1])
            at += length + 1
        else:
            if at < len(data):
                out.extend(bytes([data[at]]) * (257 - length))
                at += 1
    return bytes(out)


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------

_OBJECT = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")


class Document:
    """Every object in the file, found by scanning rather than by the index.

    A cross-reference table is a list of byte offsets, and a surprising number
    of real PDFs have wrong ones — a bank's system appends a page and forgets
    to rewrite the table. Scanning for the objects themselves cannot be wrong
    in that way, and costs nothing on a file the size of a statement.
    """

    def __init__(self, raw: bytes):
        if not raw.startswith(b"%PDF"):
            raise PdfError("That file is not a PDF.")
        self.raw = raw
        self.objects: dict[int, object] = {}
        self._scan()
        self._expand_object_streams()

    def _scan(self) -> None:
        for match in _OBJECT.finditer(self.raw):
            number = int(match.group(1))
            try:
                value, _end = parse(self.raw, match.end())
            except (ValueError, IndexError, RecursionError):
                continue
            # A later definition of the same object wins, as in an updated file.
            self.objects[number] = value

    def _expand_object_streams(self) -> None:
        """Objects hidden inside /ObjStm containers, as modern writers do."""
        for holder in list(self.objects.values()):
            if not isinstance(holder, Stream):
                continue
            if holder.info.get("Type") != "ObjStm":
                continue
            body = holder.data()
            if not body:
                continue
            count = self.get(holder.info.get("N")) or 0
            first = self.get(holder.info.get("First")) or 0
            header = body[:first].split()
            try:
                pairs = [(int(header[i]), int(header[i + 1]))
                         for i in range(0, min(len(header), count * 2), 2)]
            except (ValueError, IndexError):
                continue
            for number, offset in pairs:
                if number in self.objects:
                    continue
                try:
                    value, _end = parse(body, first + offset)
                except (ValueError, IndexError, RecursionError):
                    continue
                self.objects[number] = value

    def get(self, value, depth: int = 0):
        """Follow a reference, however many hops it takes."""
        while isinstance(value, Ref) and depth < 32:
            value = self.objects.get(value.num)
            depth += 1
        return value

    # -- pages ------------------------------------------------------------

    def pages(self) -> list[dict]:
        """Every page, in reading order where the tree can be walked."""
        found: list[dict] = []
        catalog = next((self.get(o) for o in self.objects.values()
                        if isinstance(self.get(o), dict)
                        and self.get(o).get("Type") == "Catalog"), None)
        if catalog is not None:
            root = self.get(catalog.get("Pages"))
            if isinstance(root, dict):
                self._walk(root, found, set(), {})
        if found:
            return found

        # No usable tree: take the page objects in the order they appear.
        for value in self.objects.values():
            page = self.get(value)
            if isinstance(page, dict) and page.get("Type") == "Page":
                found.append(page)
        return found

    #: What a page inherits from the node above it when it says nothing itself.
    INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")

    def _walk(self, node: dict, found: list, seen: set, inherited: dict) -> None:
        if id(node) in seen or len(found) > 2000:
            return
        seen.add(id(node))

        passed = dict(inherited)
        for key in self.INHERITED:
            if key in node:
                passed[key] = node[key]

        kind = node.get("Type")
        kids = self.get(node.get("Kids"))
        if kind == "Page" or (kids is None and "Contents" in node):
            page = dict(passed)
            page.update(node)
            found.append(page)
            return
        if isinstance(kids, list):
            for kid in kids:
                child = self.get(kid)
                if isinstance(child, dict):
                    self._walk(child, found, seen, passed)

    def content_of(self, page: dict) -> bytes:
        contents = self.get(page.get("Contents"))
        parts = []
        for item in (contents if isinstance(contents, list) else [contents]):
            stream = self.get(item)
            if isinstance(stream, Stream):
                parts.append(stream.data())
        return b"\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# What the bytes in a string actually say
# --------------------------------------------------------------------------
#
# A PDF string is a run of character codes, and what those codes mean depends
# entirely on the font they are shown in. Some fonts are one byte per
# character in a named encoding; some carry a table of exceptions; some are
# two bytes per character with a private numbering and a translation table
# bolted on. Reading the text means reading the font first.


class Font:
    """How to turn the bytes of a shown string into characters."""

    def __init__(self, two_byte: bool = False, to_unicode: dict | None = None,
                 differences: dict | None = None, base: str = "cp1252"):
        self.two_byte = two_byte
        self.to_unicode = to_unicode or {}
        self.differences = differences or {}
        self.base = base

    @property
    def readable(self) -> bool:
        """A two-byte font with no translation table cannot be read at all."""
        return not self.two_byte or bool(self.to_unicode)

    def decode(self, raw: bytes) -> str:
        if self.two_byte:
            out = []
            for i in range(0, len(raw) - 1, 2):
                code = (raw[i] << 8) | raw[i + 1]
                out.append(self.to_unicode.get(code, ""))
            return "".join(out)

        out = []
        for byte in raw:
            if byte in self.to_unicode:
                out.append(self.to_unicode[byte])
            elif byte in self.differences:
                out.append(self.differences[byte])
            else:
                try:
                    out.append(bytes([byte]).decode(self.base))
                except UnicodeDecodeError:
                    out.append("")
        return "".join(out)


#: The handful of glyph names worth translating out of a /Differences table.
#: A statement uses letters, digits and a little punctuation; anything more
#: exotic is left alone rather than guessed at.
_GLYPHS = {
    "space": " ", "comma": ",", "period": ".", "hyphen": "-", "slash": "/",
    "colon": ":", "semicolon": ";", "parenleft": "(", "parenright": ")",
    "percent": "%", "plus": "+", "minus": "-", "equal": "=", "asterisk": "*",
    "quotesingle": "'", "quotedbl": '"', "quoteright": "’",
    "quoteleft": "‘", "endash": "–", "emdash": "—",
    "sterling": "£", "dollar": "$", "euro": "€", "yen": "¥", "cent": "¢",
    "ampersand": "&", "at": "@", "numbersign": "#", "underscore": "_",
    "bullet": "•", "zero": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9",
}


def _glyph_name_to_text(name: str) -> str:
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    match = re.fullmatch(r"uni([0-9A-Fa-f]{4})", name)
    if match:
        return chr(int(match.group(1), 16))
    match = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if match:
        return chr(int(match.group(1), 16))
    return ""


_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_HEX = re.compile(rb"<([0-9A-Fa-f]+)>")


def _from_utf16(raw: bytes) -> str:
    try:
        return raw.decode("utf-16-be").replace("\x00", "")
    except UnicodeDecodeError:
        return ""


def parse_cmap(data: bytes) -> dict[int, str]:
    """A ToUnicode CMap: which code stands for which character."""
    table: dict[int, str] = {}

    for block in _BFCHAR.findall(data):
        codes = _HEX.findall(block)
        for i in range(0, len(codes) - 1, 2):
            try:
                code = int(codes[i], 16)
            except ValueError:
                continue
            table[code] = _from_utf16(bytes.fromhex(codes[i + 1].decode("ascii")))

    for block in _BFRANGE.findall(data):
        # Two shapes: <lo> <hi> <start>, and <lo> <hi> [<a> <b> ...]
        for match in re.finditer(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)",
                block, re.S):
            try:
                low, high = int(match.group(1), 16), int(match.group(2), 16)
            except ValueError:
                continue
            if high < low or high - low > 65535:
                continue
            target = match.group(3)
            if target.startswith(b"["):
                for offset, item in enumerate(_HEX.findall(target)):
                    table[low + offset] = _from_utf16(
                        bytes.fromhex(item.decode("ascii")))
            else:
                start = _from_utf16(bytes.fromhex(
                    _HEX.match(target).group(1).decode("ascii")))
                if not start:
                    continue
                for offset in range(high - low + 1):
                    table[low + offset] = start[:-1] + chr(ord(start[-1]) + offset)
    return table


def read_font(doc: "Document", value) -> Font:
    info = doc.get(value)
    if not isinstance(info, dict):
        return Font()

    to_unicode: dict[int, str] = {}
    stream = doc.get(info.get("ToUnicode"))
    if isinstance(stream, Stream):
        to_unicode = parse_cmap(stream.data())

    subtype = info.get("Subtype")
    encoding = doc.get(info.get("Encoding"))
    two_byte = subtype == "Type0"
    if isinstance(encoding, str) and encoding in ("Identity-H", "Identity-V"):
        two_byte = True

    base = "cp1252"
    differences: dict[int, str] = {}
    if isinstance(encoding, str) and encoding == "MacRomanEncoding":
        base = "mac_roman"
    if isinstance(encoding, dict):
        if encoding.get("BaseEncoding") == "MacRomanEncoding":
            base = "mac_roman"
        table = doc.get(encoding.get("Differences"))
        if isinstance(table, list):
            code = 0
            for item in table:
                if isinstance(item, (int, float)):
                    code = int(item)
                elif isinstance(item, str):
                    text = _glyph_name_to_text(item)
                    if text:
                        differences[code] = text
                    code += 1

    return Font(two_byte=two_byte, to_unicode=to_unicode,
                differences=differences, base=base)


def fonts_of(doc: "Document", page: dict) -> dict[str, Font]:
    resources = doc.get(page.get("Resources"))
    if not isinstance(resources, dict):
        return {}
    table = doc.get(resources.get("Font"))
    if not isinstance(table, dict):
        return {}
    return {name: read_font(doc, value) for name, value in table.items()}


# --------------------------------------------------------------------------
# Where each piece of text sits on the page
# --------------------------------------------------------------------------


@dataclass
class Piece:
    """Some text, and the point on the page where it starts."""

    x: float
    y: float
    text: str
    size: float = 0.0


def _multiply(a: tuple, b: tuple) -> tuple:
    """Two 2×3 transformation matrices, combined."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a1 * b2, a0 * b1 + a1 * b3,
            a2 * b0 + a3 * b2, a2 * b1 + a3 * b3,
            a4 * b0 + a5 * b2 + b4, a4 * b1 + a5 * b3 + b5)


_TOKEN = re.compile(rb"""
      (?P<string>\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\))
    | (?P<hex><[0-9A-Fa-f\s]*>)
    | (?P<name>/[^\s/\[\]<>(){}%]*)
    | (?P<number>[+-]?(?:\d+\.?\d*|\.\d+))
    | (?P<array>\[)
    | (?P<endarray>\])
    | (?P<dict><<)
    | (?P<enddict>>>)
    | (?P<operator>[A-Za-z'"*][A-Za-z0-9*'"]*)
""", re.X | re.S)


def pieces_of(content: bytes, fonts: dict[str, Font]) -> list[Piece]:
    """Walk a page's instructions and note every piece of text it draws.

    Only the text operators matter, but the graphics state does too: a
    statement often draws its table inside a translated coordinate system, and
    a piece placed at (0, 0) of that system is not at the corner of the page.
    """
    pieces: list[Piece] = []
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    ctm = identity
    stack: list[tuple] = []
    text_matrix = line_matrix = identity
    font = Font()
    size = 10.0
    leading = 0.0

    operands: list = []
    depth = 0

    for match in _TOKEN.finditer(content):
        kind = match.lastgroup
        raw = match.group(0)

        if kind == "array":
            depth += 1
            operands.append([])
            continue
        if kind == "endarray":
            depth = max(0, depth - 1)
            continue
        if kind in ("dict", "enddict"):
            continue

        if kind == "string":
            value, _ = _literal_string(raw, 0)
        elif kind == "hex":
            value, _ = _hex_string(raw, 0)
        elif kind == "name":
            value = raw[1:].decode("latin-1")
        elif kind == "number":
            value = float(raw)
        elif kind == "operator":
            operator = raw.decode("latin-1")

            if operator == "q":
                stack.append(ctm)
            elif operator == "Q":
                ctm = stack.pop() if stack else identity
            elif operator == "cm" and len(operands) >= 6:
                ctm = _multiply(tuple(float(v) for v in operands[-6:]), ctm)
            elif operator == "BT":
                text_matrix = line_matrix = identity
            elif operator == "Tf" and len(operands) >= 2:
                font = fonts.get(str(operands[-2]), Font())
                try:
                    size = float(operands[-1])
                except (TypeError, ValueError):
                    size = 10.0
            elif operator == "TL" and operands:
                leading = float(operands[-1])
            elif operator == "Tm" and len(operands) >= 6:
                text_matrix = line_matrix = tuple(float(v) for v in operands[-6:])
            elif operator in ("Td", "TD") and len(operands) >= 2:
                if operator == "TD":
                    leading = -float(operands[-1])
                shift = (1.0, 0.0, 0.0, 1.0, float(operands[-2]), float(operands[-1]))
                text_matrix = line_matrix = _multiply(shift, line_matrix)
            elif operator == "T*":
                shift = (1.0, 0.0, 0.0, 1.0, 0.0, -leading)
                text_matrix = line_matrix = _multiply(shift, line_matrix)
            elif operator in ("Tj", "TJ", "'", '"'):
                if operator in ("'", '"'):
                    shift = (1.0, 0.0, 0.0, 1.0, 0.0, -leading)
                    text_matrix = line_matrix = _multiply(shift, line_matrix)

                shown = operands[-1] if operands else b""
                parts: list[bytes] = []
                if isinstance(shown, list):
                    parts = [p for p in shown if isinstance(p, bytes)]
                elif isinstance(shown, bytes):
                    parts = [shown]

                text = "".join(font.decode(p) for p in parts)
                if text.strip():
                    placed = _multiply(text_matrix, ctm)
                    pieces.append(Piece(x=placed[4], y=placed[5], text=text,
                                        size=abs(size * placed[3] or size)))
            operands = []
            depth = 0
            continue
        else:
            continue

        if depth and operands and isinstance(operands[-1], list):
            operands[-1].append(value)
        else:
            operands.append(value)
        if len(operands) > 32:                # a runaway operand list
            operands = operands[-16:]

    return pieces


# --------------------------------------------------------------------------
# From scattered pieces to rows and columns
# --------------------------------------------------------------------------


def rows_of(pieces: list[Piece]) -> list[list[Piece]]:
    """Group pieces that share a baseline, top of the page first."""
    rows: list[list[Piece]] = []
    for piece in sorted(pieces, key=lambda p: (-p.y, p.x)):
        if rows and abs(rows[-1][0].y - piece.y) <= LINE_TOLERANCE:
            rows[-1].append(piece)
        else:
            rows.append([piece])
    for row in rows:
        row.sort(key=lambda p: p.x)
    return rows


def _width_of(piece: "Piece") -> float:
    """Roughly how wide this piece is, so its right-hand edge is known.

    An approximation on purpose: the exact answer needs the font's own metric
    table, and what this is used for — telling one column from the next across
    a gap of ten points or more — does not repay reading it.
    """
    from .pdfwriter import WIDTHS, HELVETICA

    table = WIDTHS[HELVETICA]
    thousandths = sum(table.get(ord(ch), 500) for ch in piece.text)
    return thousandths * (piece.size or 10.0) / 1000.0


def columns_of(rows: list[list[Piece]]) -> list[tuple[float, float]]:
    """Where the columns are, found by looking for the gaps between them.

    Column starts are no good on their own: a bank right-aligns its figures,
    so ₦45,500.00 and ₦1,244,500.00 begin at different places and end at the
    same one. What every column shares, whichever way its contents are
    aligned, is the strip of blank page down each side of it — so the columns
    are found by looking for the vertical gutters that no text ever crosses.

    Only rows with several pieces on them are consulted. A title runs the
    width of the page and would paper over every gutter there is.
    """
    table_rows = [r for r in rows if len(r) >= 3] or rows
    spans = sorted((p.x, p.x + _width_of(p)) for row in table_rows for p in row)
    if not spans:
        return []

    # Merge the spans into the strips of page that have text on them.
    covered: list[list[float]] = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= covered[-1][1] + GUTTER:
            covered[-1][1] = max(covered[-1][1], end)
        else:
            covered.append([start, end])

    if len(covered) < 2:
        # One block of text: fall back to where pieces start.
        starts = sorted({round(p.x, 1) for row in table_rows for p in row})
        return [(x, x + COLUMN_TOLERANCE) for x in starts]

    # A column runs from its own left edge to the start of the next gutter.
    edges: list[tuple[float, float]] = []
    for index, (start, end) in enumerate(covered):
        after = covered[index + 1][0] if index + 1 < len(covered) else end + 1000
        edges.append((start - GUTTER / 2, (end + after) / 2))
    return edges


def _column_for(piece: "Piece", columns: list[tuple[float, float]]) -> int:
    """Which column this piece belongs to: the one its span sits inside."""
    left = piece.x
    right = left + _width_of(piece)
    middle = (left + right) / 2
    for index, (start, end) in enumerate(columns):
        if start <= middle <= end:
            return index
    # Outside every column: give it to the nearest one.
    best, distance = 0, None
    for index, (start, end) in enumerate(columns):
        gap = 0.0 if start <= left <= end else min(abs(left - start), abs(left - end))
        if distance is None or gap < distance:
            best, distance = index, gap
    return best


def grid_of(pieces: list[Piece]) -> list[list[str]]:
    """The page as a table of text, ready to be read like a spreadsheet."""
    rows = rows_of(pieces)
    columns = columns_of(rows)
    if not columns:
        return []

    out: list[list[str]] = []
    for row in rows:
        cells = [""] * len(columns)
        for piece in row:
            at = _column_for(piece, columns)
            cells[at] = (cells[at] + " " + piece.text).strip() if cells[at] \
                else piece.text
        out.append(cells)
    return out


def looks_scanned(doc: Document, recovered: str) -> bool:
    """A picture of a statement rather than a statement."""
    if len(recovered.strip()) >= ENOUGH_TEXT:
        return False
    for value in doc.objects.values():
        item = doc.get(value)
        if isinstance(item, Stream) and item.info.get("Subtype") == "Image":
            return True
    return True


def read(raw: bytes) -> list[list[str]]:
    """Every page of a PDF, as one table of text.

    Pages are stacked one after another because a statement that runs to three
    pages is one statement, and the heading only appears on the first.
    """
    doc = Document(raw)
    pages = doc.pages()
    if not pages:
        raise PdfError("No pages could be found in that PDF.")

    everything: list[list[str]] = []
    recovered: list[str] = []
    unreadable_font = False

    for page in pages[:60]:                   # a statement is not a book
        fonts = fonts_of(doc, page)
        if any(not f.readable for f in fonts.values()):
            unreadable_font = True
        pieces = pieces_of(doc.content_of(page), fonts)
        recovered.extend(p.text for p in pieces)
        everything.extend(grid_of(pieces))

    text = "".join(recovered)
    if looks_scanned(doc, text):
        if unreadable_font:
            raise PdfError(
                "That PDF's text cannot be extracted — the file stores its "
                "letters without saying what they mean, which some bank "
                "systems do deliberately. Download the statement as CSV or "
                "Excel from your internet banking instead.")
        raise PdfError(
            "There is no text in that PDF — it is a scan or a photograph of a "
            "statement, so the figures in it are pictures. Download the "
            "statement as CSV or Excel from your internet banking instead.")
    return everything
