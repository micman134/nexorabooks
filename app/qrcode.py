"""QR codes, drawn as SVG, with nothing installed.

Written for one job: showing an ``otpauth://`` string on the two-factor setup
screen so somebody can point their phone at it. It is a real QR encoder to the
ISO/IEC 18004 rules — byte mode, error correction level M, versions 1 to 10 —
rather than a picture fetched from somewhere, because fetching it would mean
sending the person's two-factor secret to a stranger's server, and this software
promises it sends nothing anywhere. That promise is worth more than the four
hundred lines below.

Level M corrects about 15% of the symbol, which is the level every
authenticator app expects and enough to survive a phone camera at an angle.
Versions 1–10 hold up to 213 bytes; an ``otpauth`` URI is about 150.

The output is SVG, so it stays sharp at any size, prints properly, and needs no
image library on either side.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Arithmetic in GF(256) — the field Reed-Solomon works over
# --------------------------------------------------------------------------
#
# QR uses the primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D). Building
# the log and antilog tables once turns every multiply below into two lookups
# and an addition.

_EXP = [0] * 512
_LOG = [0] * 256


def _build_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """The generator polynomial for ``degree`` error-correction codewords."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            nxt[j] ^= coeff
            nxt[j + 1] ^= _mul(coeff, _EXP[i])
        poly = nxt
    return poly


def _remainder(data: list[int], degree: int) -> list[int]:
    """The error-correction codewords for one block: polynomial long division."""
    gen = _generator(degree)
    residue = list(data) + [0] * degree
    for i in range(len(data)):
        factor = residue[i]
        if factor == 0:
            continue
        for j, coeff in enumerate(gen):
            residue[i + j] ^= _mul(coeff, factor)
    return residue[len(data):]


# --------------------------------------------------------------------------
# What each version holds, at error-correction level M
# --------------------------------------------------------------------------
#
# (ec codewords per block, blocks in group 1, data codewords each,
#  blocks in group 2, data codewords each). Straight from ISO/IEC 18004
# Table 9; there is a test that proves each row adds up to the version's
# total capacity, which is what catches a typo here.

_BLOCKS_M: dict[int, tuple[int, int, int, int, int]] = {
    1:  (10, 1, 16, 0, 0),
    2:  (16, 1, 28, 0, 0),
    3:  (26, 1, 44, 0, 0),
    4:  (18, 2, 32, 0, 0),
    5:  (24, 2, 43, 0, 0),
    6:  (16, 4, 27, 0, 0),
    7:  (18, 4, 31, 0, 0),
    8:  (22, 2, 38, 2, 39),
    9:  (22, 3, 36, 2, 37),
    10: (26, 4, 43, 1, 44),
}

#: Total codewords (data plus error correction) in each version.
_TOTAL_CODEWORDS = {
    1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172, 7: 196, 8: 242, 9: 292, 10: 346,
}

#: Where the alignment patterns are centred, by version.
_ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

#: Error-correction level M, as the two bits that go into the format string.
_EC_BITS = 0b00

MAX_VERSION = 10


def data_capacity(version: int) -> int:
    """Data codewords available at this version, level M."""
    ec, g1, d1, g2, d2 = _BLOCKS_M[version]
    return g1 * d1 + g2 * d2


def byte_capacity(version: int) -> int:
    """How many bytes of payload fit, after the header and length field."""
    header_bits = 4 + (8 if version < 10 else 16)
    return (data_capacity(version) * 8 - header_bits) // 8


def fits(text: str) -> bool:
    """True when this text can be drawn as a QR code at all."""
    return len(text.encode("utf-8")) <= byte_capacity(MAX_VERSION)


# --------------------------------------------------------------------------
# Turning the text into codewords
# --------------------------------------------------------------------------


class QRError(ValueError):
    """The text will not fit, or cannot be encoded."""


def _smallest_version(length: int) -> int:
    for version in range(1, MAX_VERSION + 1):
        if length <= byte_capacity(version):
            return version
    raise QRError(
        f"{length} bytes is more than a version {MAX_VERSION} QR code holds "
        f"({byte_capacity(MAX_VERSION)})."
    )


class _Bits:
    """A bit string being built up, MSB first."""

    def __init__(self) -> None:
        self.bits: list[int] = []

    def put(self, value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def __len__(self) -> int:
        return len(self.bits)

    def codewords(self) -> list[int]:
        out = []
        for i in range(0, len(self.bits), 8):
            byte = 0
            for bit in self.bits[i:i + 8]:
                byte = (byte << 1) | bit
            out.append(byte)
        return out


def _pad_to_capacity(words: list[int], capacity: int) -> list[int]:
    """Fill the rest of the block with the standard alternating pad bytes."""
    pad = (0xEC, 0x11)
    out = list(words)
    i = 0
    while len(out) < capacity:
        out.append(pad[i % 2])
        i += 1
    return out


def _codewords(payload: bytes, version: int) -> list[int]:
    """Data codewords and error-correction codewords, interleaved as required."""
    capacity = data_capacity(version)
    bits = _Bits()
    bits.put(0b0100, 4)
    bits.put(len(payload), 8 if version < 10 else 16)
    for byte in payload:
        bits.put(byte, 8)
    for _ in range(min(4, capacity * 8 - len(bits))):
        bits.put(0, 1)
    while len(bits) % 8:
        bits.put(0, 1)
    data = _pad_to_capacity(bits.codewords(), capacity)

    ec_count, g1, d1, g2, d2 = _BLOCKS_M[version]
    blocks: list[list[int]] = []
    at = 0
    for _ in range(g1):
        blocks.append(data[at:at + d1])
        at += d1
    for _ in range(g2):
        blocks.append(data[at:at + d2])
        at += d2
    ec_blocks = [_remainder(block, ec_count) for block in blocks]

    # Interleaved: first codeword of every block, then the second, and so on.
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_count):
        for block in ec_blocks:
            out.append(block[i])
    return out


# --------------------------------------------------------------------------
# Laying out the symbol
# --------------------------------------------------------------------------

LIGHT, DARK = 0, 1


class _Grid:
    def __init__(self, version: int):
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[LIGHT] * self.size for _ in range(self.size)]
        #: True where a function pattern lives, so data never overwrites one.
        self.reserved = [[False] * self.size for _ in range(self.size)]

    def set(self, row: int, col: int, value: int, reserve: bool = True) -> None:
        self.modules[row][col] = value
        if reserve:
            self.reserved[row][col] = True

    # -- the fixed patterns ------------------------------------------------
    def _finder(self, row: int, col: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if not (0 <= rr < self.size and 0 <= cc < self.size):
                    continue
                edge = max(abs(r - 3), abs(c - 3))
                # Rings: dark 3, light 2, dark 0-1 — plus the light separator.
                self.set(rr, cc, DARK if edge in (0, 1, 3) else LIGHT)

    def _timing(self) -> None:
        for i in range(8, self.size - 8):
            value = DARK if i % 2 == 0 else LIGHT
            self.set(6, i, value)
            self.set(i, 6, value)

    def _alignment(self) -> None:
        centres = _ALIGNMENT[self.version]
        for row in centres:
            for col in centres:
                # Not where a finder already is.
                if (row, col) in ((6, 6), (6, self.size - 7), (self.size - 7, 6)):
                    continue
                for r in range(-2, 3):
                    for c in range(-2, 3):
                        edge = max(abs(r), abs(c))
                        self.set(row + r, col + c, DARK if edge != 1 else LIGHT)

    def _reserve_format(self) -> None:
        for i in range(9):
            if i != 6:
                self.set(8, i, LIGHT)
                self.set(i, 8, LIGHT)
        for i in range(8):
            self.set(8, self.size - 1 - i, LIGHT)
            self.set(self.size - 1 - i, 8, LIGHT)
        # The one module that is always dark, and is not part of the format.
        self.set(self.size - 8, 8, DARK)

    def _reserve_version(self) -> None:
        if self.version < 7:
            return
        for i in range(6):
            for j in range(3):
                self.set(self.size - 11 + j, i, LIGHT)
                self.set(i, self.size - 11 + j, LIGHT)

    def build_function_patterns(self) -> None:
        self._finder(0, 0)
        self._finder(0, self.size - 7)
        self._finder(self.size - 7, 0)
        self._timing()
        self._alignment()
        self._reserve_version()
        self._reserve_format()

    # -- the data ----------------------------------------------------------
    def place_data(self, codewords: list[int]) -> None:
        """Two modules wide, upwards then downwards, right to left."""
        bits = [(word >> shift) & 1 for word in codewords for shift in range(7, -1, -1)]
        index = 0
        upward = True
        col = self.size - 1
        while col > 0:
            if col == 6:       # the vertical timing pattern is skipped entirely
                col -= 1
            rows = range(self.size - 1, -1, -1) if upward else range(self.size)
            for row in rows:
                for offset in (0, 1):
                    c = col - offset
                    if self.reserved[row][c]:
                        continue
                    bit = bits[index] if index < len(bits) else 0
                    index += 1
                    self.modules[row][c] = bit
            upward = not upward
            col -= 2

    # -- masking -----------------------------------------------------------
    @staticmethod
    def _mask(pattern: int, row: int, col: int) -> bool:
        if pattern == 0:
            return (row + col) % 2 == 0
        if pattern == 1:
            return row % 2 == 0
        if pattern == 2:
            return col % 3 == 0
        if pattern == 3:
            return (row + col) % 3 == 0
        if pattern == 4:
            return (row // 2 + col // 3) % 2 == 0
        if pattern == 5:
            return (row * col) % 2 + (row * col) % 3 == 0
        if pattern == 6:
            return ((row * col) % 2 + (row * col) % 3) % 2 == 0
        return ((row + col) % 2 + (row * col) % 3) % 2 == 0

    def masked(self, pattern: int) -> list[list[int]]:
        out = [row[:] for row in self.modules]
        for r in range(self.size):
            for c in range(self.size):
                if not self.reserved[r][c] and self._mask(pattern, r, c):
                    out[r][c] ^= 1
        return out


# --------------------------------------------------------------------------
# Format information, and choosing a mask
# --------------------------------------------------------------------------


def _bch(value: int, generator: int, width: int) -> int:
    """Append BCH check bits to ``value``."""
    result = value << width
    generator_bits = generator.bit_length()
    while result.bit_length() >= generator_bits:
        result ^= generator << (result.bit_length() - generator_bits)
    return (value << width) | result


def _format_bits(mask: int) -> int:
    data = (_EC_BITS << 3) | mask
    return _bch(data, 0b10100110111, 10) ^ 0b101010000010010


def _version_bits(version: int) -> int:
    return _bch(version, 0b1111100100101, 12)


def _penalty(grid: list[list[int]]) -> int:
    """The four scoring rules from the standard. Lower is better."""
    size = len(grid)
    score = 0

    # Rule 1 — runs of five or more of the same colour.
    for line in list(grid) + [list(col) for col in zip(*grid)]:
        run, previous = 1, line[0]
        for value in line[1:]:
            if value == previous:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, previous = 1, value
        if run >= 5:
            score += 3 + (run - 5)

    # Rule 2 — blocks of two by two in one colour.
    for r in range(size - 1):
        for c in range(size - 1):
            block = (grid[r][c], grid[r][c + 1], grid[r + 1][c], grid[r + 1][c + 1])
            if block[0] == block[1] == block[2] == block[3]:
                score += 3

    # Rule 3 — the finder-like pattern appearing in the data.
    wanted = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])
    for line in list(grid) + [list(col) for col in zip(*grid)]:
        for i in range(size - 10):
            window = list(line[i:i + 11])
            if window in wanted:
                score += 40

    # Rule 4 — how far the dark proportion is from half.
    dark = sum(sum(row) for row in grid)
    percent = dark * 100 / (size * size)
    score += 10 * int(abs(percent - 50) // 5)
    return score


def _apply_format(grid: _Grid, modules: list[list[int]], mask: int) -> None:
    bits = _format_bits(mask)
    size = grid.size
    for i in range(15):
        # Most significant bit first: bit 14 lands at (8, 0) and bit 0 at the
        # far end of each copy. Getting this backwards produces a symbol that
        # is structurally perfect and completely unreadable, which is a
        # remarkably difficult thing to notice by eye.
        bit = (bits >> (14 - i)) & 1
        # The copy beside the top-left finder.
        if i < 6:
            modules[8][i] = bit
        elif i == 6:
            modules[8][7] = bit
        elif i == 7:
            modules[8][8] = bit
        elif i == 8:
            modules[7][8] = bit
        else:
            modules[14 - i][8] = bit
        # The second copy, split between the other two corners: seven modules
        # up the bottom-left finder and eight along the top-right. Seven and
        # eight, not eight and seven — the module at (size-8, 8) is the
        # always-dark one and belongs to neither copy.
        if i < 7:
            modules[size - 1 - i][8] = bit
        else:
            modules[8][size - 15 + i] = bit

    if grid.version >= 7:
        version_bits = _version_bits(grid.version)
        for i in range(18):
            bit = (version_bits >> i) & 1
            row, col = i // 3, i % 3
            modules[size - 11 + col][row] = bit
            modules[row][size - 11 + col] = bit


# --------------------------------------------------------------------------
# The public part
# --------------------------------------------------------------------------


def matrix(text: str) -> list[list[int]]:
    """The finished symbol as rows of 0 and 1, without a quiet zone."""
    payload = text.encode("utf-8")
    version = _smallest_version(len(payload))
    grid = _Grid(version)
    grid.build_function_patterns()
    grid.place_data(_codewords(payload, version))

    best, best_score = None, None
    for mask in range(8):
        candidate = grid.masked(mask)
        _apply_format(grid, candidate, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


def svg(text: str, *, module: int = 6, quiet: int = 4,
        dark: str = "#000000", light: str = "#ffffff",
        title: str = "") -> str:
    """An SVG image of the code, ready to drop straight into a page.

    ``quiet`` is the blank margin, in modules. Four is the standard minimum and
    scanners genuinely need it — a code butted against other content often will
    not read.
    """
    grid = matrix(text)
    size = len(grid) + quiet * 2
    px = size * module

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
        f'viewBox="0 0 {size} {size}" shape-rendering="crispEdges" role="img"'
        + (f' aria-label="{title}"' if title else ' aria-hidden="true"') + ">",
        f'<rect width="{size}" height="{size}" fill="{light}"/>',
    ]
    # One path for every dark module keeps the file small and scales cleanly.
    runs = []
    for r, row in enumerate(grid):
        c = 0
        while c < len(row):
            if row[c]:
                start = c
                while c < len(row) and row[c]:
                    c += 1
                runs.append(f"M{start + quiet} {r + quiet}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    parts.append(f'<path fill="{dark}" d="{"".join(runs)}"/>')
    parts.append("</svg>")
    return "".join(parts)
