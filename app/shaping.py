"""Making right-to-left text come out right on paper.

Embedding a font is enough for Chinese, Greek, Cyrillic, Thai and most of the
world. Arabic is the exception, and it is not a small one: Arabic letters change
shape according to what stands beside them, and the line runs from the right.
An Arabic name printed one letter at a time, left to right, in isolated forms
is not "slightly wrong" — it is unreadable, in the way ``d e t c e n n o c s i d``
is unreadable. So it is worth doing properly.

Two jobs, in this order:

  1. **Joining.** Each letter is replaced by its initial, medial, final or
     isolated form depending on whether its neighbours join to it, and the
     lam-alef pairs are replaced by the single ligature Arabic requires.
     The forms come out of the Unicode presentation block, which the fonts
     that ship with Windows, macOS and Linux all contain.
  2. **Ordering.** The text is reordered so that a reader gets it in the right
     sequence when the PDF lays glyphs out left to right. Latin words and
     numbers embedded in Arabic keep their own direction, which is what makes
     an invoice number inside an Arabic address still read as that number.

This is not a full implementation of the Unicode bidirectional algorithm, and
it does not pretend to be. It handles what appears on invoices — a name, an
address, a phone number, a description — and it says so here rather than
letting somebody find out.

Hebrew needs no joining, only the reordering, and gets it.
"""
from __future__ import annotations

#: base code point -> (isolated, final) or (isolated, final, initial, medial).
#: A four-entry row is a letter that joins on both sides.
_FORMS: dict[int, tuple[int, ...]] = {
    0x0621: (0xFE80,),
    0x0622: (0xFE81, 0xFE82),
    0x0623: (0xFE83, 0xFE84),
    0x0624: (0xFE85, 0xFE86),
    0x0625: (0xFE87, 0xFE88),
    0x0626: (0xFE89, 0xFE8A, 0xFE8B, 0xFE8C),
    0x0627: (0xFE8D, 0xFE8E),
    0x0628: (0xFE8F, 0xFE90, 0xFE91, 0xFE92),
    0x0629: (0xFE93, 0xFE94),
    0x062A: (0xFE95, 0xFE96, 0xFE97, 0xFE98),
    0x062B: (0xFE99, 0xFE9A, 0xFE9B, 0xFE9C),
    0x062C: (0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0),
    0x062D: (0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4),
    0x062E: (0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8),
    0x062F: (0xFEA9, 0xFEAA),
    0x0630: (0xFEAB, 0xFEAC),
    0x0631: (0xFEAD, 0xFEAE),
    0x0632: (0xFEAF, 0xFEB0),
    0x0633: (0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4),
    0x0634: (0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8),
    0x0635: (0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC),
    0x0636: (0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0),
    0x0637: (0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4),
    0x0638: (0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8),
    0x0639: (0xFEC9, 0xFECA, 0xFECB, 0xFECC),
    0x063A: (0xFECD, 0xFECE, 0xFECF, 0xFED0),
    0x0641: (0xFED1, 0xFED2, 0xFED3, 0xFED4),
    0x0642: (0xFED5, 0xFED6, 0xFED7, 0xFED8),
    0x0643: (0xFED9, 0xFEDA, 0xFEDB, 0xFEDC),
    0x0644: (0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0),
    0x0645: (0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4),
    0x0646: (0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8),
    0x0647: (0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC),
    0x0648: (0xFEED, 0xFEEE),
    0x0649: (0xFEEF, 0xFEF0),
    0x064A: (0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4),
    # Persian, Urdu and Pashto letters, from the extended presentation block.
    0x0671: (0xFB50, 0xFB51),
    0x0679: (0xFB66, 0xFB67, 0xFB68, 0xFB69),
    0x067E: (0xFB56, 0xFB57, 0xFB58, 0xFB59),
    0x0686: (0xFB7A, 0xFB7B, 0xFB7C, 0xFB7D),
    0x0688: (0xFB88, 0xFB89),
    0x0691: (0xFB8C, 0xFB8D),
    0x0698: (0xFB8A, 0xFB8B),
    0x06A9: (0xFB8E, 0xFB8F, 0xFB90, 0xFB91),
    0x06AF: (0xFB92, 0xFB93, 0xFB94, 0xFB95),
    0x06BA: (0xFB9E, 0xFB9F),
    0x06BE: (0xFBAA, 0xFBAB, 0xFBAC, 0xFBAD),
    0x06C0: (0xFBA4, 0xFBA5),
    0x06C1: (0xFBA6, 0xFBA7, 0xFBA8, 0xFBA9),
    0x06CC: (0xFBFC, 0xFBFD, 0xFBFE, 0xFBFF),
    0x06D2: (0xFBAE, 0xFBAF),
    0x06D3: (0xFBB0, 0xFBB1),
}

#: lam followed by one of these becomes a single ligature: (isolated, final).
_LAM_ALEF = {
    0x0622: (0xFEF5, 0xFEF6),
    0x0623: (0xFEF7, 0xFEF8),
    0x0625: (0xFEF9, 0xFEFA),
    0x0627: (0xFEFB, 0xFEFC),
}

#: Vowel signs and other marks. They sit above or below a letter and take no
#: part in joining, so the letters on either side of them still join to
#: each other.
_TRANSPARENT = (
    (0x0610, 0x061A), (0x064B, 0x065F), (0x0670, 0x0670),
    (0x06D6, 0x06DC), (0x06DF, 0x06E4), (0x06E7, 0x06E8),
    (0x06EA, 0x06ED), (0x200B, 0x200B), (0x200E, 0x200F),
    (0xFE00, 0xFE0F), (0x0300, 0x036F),
)

_TATWEEL = 0x0640


def _is_transparent(code: int) -> bool:
    return any(low <= code <= high for low, high in _TRANSPARENT)


def _joins_left(code: int) -> bool:
    """Whether this character joins to the letter that follows it."""
    if code == _TATWEEL:
        return True
    forms = _FORMS.get(code)
    return bool(forms) and len(forms) == 4


def _joins_right(code: int) -> bool:
    """Whether this character joins to the letter before it."""
    if code == _TATWEEL:
        return True
    forms = _FORMS.get(code)
    return bool(forms) and len(forms) >= 2


def _is_arabic(code: int) -> bool:
    return (0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F
            or 0xFB50 <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF)


def _is_hebrew(code: int) -> bool:
    return 0x0590 <= code <= 0x05FF or 0xFB1D <= code <= 0xFB4F


def is_rtl(text: str) -> bool:
    return any(_is_arabic(ord(ch)) or _is_hebrew(ord(ch)) for ch in text)


def has_arabic(text: str) -> bool:
    return any(_is_arabic(ord(ch)) for ch in text)


def join(text: str, can_draw=None) -> str:
    """Replace Arabic letters with the shape they take in this word.

    ``can_draw`` is asked whether the font actually has a given form. A font
    without the presentation forms keeps the plain letters instead of being
    handed characters it would print as empty boxes.
    """
    if not has_arabic(text):
        return text

    codes = [ord(ch) for ch in text]
    out: list[str] = []

    def previous_joining(at: int) -> bool:
        """Does the letter before position ``at`` reach forward to it?"""
        i = at - 1
        while i >= 0 and _is_transparent(codes[i]):
            i -= 1
        return i >= 0 and _joins_left(codes[i])

    def next_joining(at: int) -> bool:
        """Does the letter after position ``at`` reach back to it?"""
        i = at + 1
        while i < len(codes) and _is_transparent(codes[i]):
            i += 1
        return i < len(codes) and _joins_right(codes[i])

    def drawable(code: int) -> bool:
        return can_draw is None or can_draw(chr(code))

    index = 0
    while index < len(codes):
        code = codes[index]

        # Lam followed by an alef is one glyph in Arabic, not two.
        if code == 0x0644 and index + 1 < len(codes) and codes[index + 1] in _LAM_ALEF:
            isolated, final = _LAM_ALEF[codes[index + 1]]
            chosen = final if previous_joining(index) else isolated
            if drawable(chosen):
                out.append(chr(chosen))
                index += 2
                continue

        forms = _FORMS.get(code)
        if not forms:
            out.append(chr(code))
            index += 1
            continue

        before = previous_joining(index)
        after = next_joining(index)
        if len(forms) == 4:
            chosen = forms[{(False, False): 0, (True, False): 1,
                            (False, True): 2, (True, True): 3}[(before, after)]]
        elif len(forms) == 2:
            chosen = forms[1] if before else forms[0]
        else:
            chosen = forms[0]
        out.append(chr(chosen) if drawable(chosen) else chr(code))
        index += 1

    return "".join(out)


_MIRRORED = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{",
             "<": ">", ">": "<", "«": "»", "»": "«"}

_ARABIC_DIGITS = ((0x0660, 0x0669), (0x066B, 0x066C), (0x06F0, 0x06F9))


def _initial_class(char: str) -> str:
    """The Unicode direction class of one character, as far as we need it.

    L  left-to-right letter        R   Hebrew letter
    AL Arabic letter               EN  European digit
    AN Arabic-Indic digit          NSM a mark that sits on the letter before it
    ES + and -                     CS  the , . : / that appear inside numbers
    WS a space                     ON  anything else
    """
    code = ord(char)
    if any(low <= code <= high for low, high in _ARABIC_DIGITS):
        return "AN"
    if code == 0x060C:                    # the Arabic comma is punctuation
        return "CS"
    if _is_transparent(code):
        return "NSM"
    if _is_arabic(code):
        return "AL"
    if _is_hebrew(code):
        return "R"
    if char.isdigit() and code < 0x0590:
        return "EN"
    if char.isalpha():
        return "L"
    if char in "+-":
        return "ES"
    if char in "#$%°‰¢£¤¥€₦₹":
        return "ET"                       # sits beside a number and joins it
    if char in ",.:/ ":
        return "CS"
    if char in " \t\n\r":
        return "WS"
    return "ON"


def _resolve(text: str, base: str = "R") -> list[str]:
    """Work out which way each character faces.

    This is the Unicode bidirectional algorithm's weak and neutral rules, kept
    to the parts that decide the answer for the text that appears on invoices:
    names, addresses, reference numbers and amounts. Getting these right is
    what makes "INV-2043" stay the right way round inside an Arabic sentence
    while "0803 123 4567" is grouped the way every other program groups it.
    """
    classes = [_initial_class(ch) for ch in text]

    # W1: a mark takes the direction of the letter it sits on.
    previous = base
    for i, kind in enumerate(classes):
        if kind == "NSM":
            classes[i] = previous
        previous = classes[i]

    # W2: European digits after an Arabic letter are Arabic digits.
    strong = base
    for i, kind in enumerate(classes):
        if kind in ("L", "R", "AL"):
            strong = kind
        elif kind == "EN" and strong == "AL":
            classes[i] = "AN"

    # W3: from here an Arabic letter is simply right-to-left.
    classes = ["R" if k == "AL" else k for k in classes]

    # W4: one separator between two digits of the same kind is part of them.
    for i in range(1, len(classes) - 1):
        before, here, after = classes[i - 1], classes[i], classes[i + 1]
        if here == "ES" and before == "EN" == after:
            classes[i] = "EN"
        elif here == "CS" and before == after and before in ("EN", "AN"):
            classes[i] = before

    # W5: a percent or currency sign beside a number is part of the number.
    i = 0
    while i < len(classes):
        if classes[i] == "ET":
            end = i
            while end < len(classes) and classes[end] == "ET":
                end += 1
            beside = (i and classes[i - 1] == "EN") or (
                end < len(classes) and classes[end] == "EN")
            if beside:
                for j in range(i, end):
                    classes[j] = "EN"
            i = end
        else:
            i += 1

    # W6: whatever is left of those is an ordinary neutral.
    classes = ["ON" if k in ("ES", "ET", "CS") else k for k in classes]

    # W7: digits that follow Latin text belong to it.
    strong = base
    for i, kind in enumerate(classes):
        if kind in ("L", "R"):
            strong = kind
        elif kind == "EN" and strong == "L":
            classes[i] = "L"

    # N1/N2: a run of neutrals between two sides that agree takes their
    # direction; otherwise it takes the paragraph's. Digits count as
    # right-to-left for this purpose.
    def side(kind: str) -> str:
        return "R" if kind in ("R", "EN", "AN") else kind

    i = 0
    while i < len(classes):
        if classes[i] in ("WS", "ON", "ES", "CS"):
            end = i
            while end < len(classes) and classes[end] in ("WS", "ON", "ES", "CS"):
                end += 1
            before = side(classes[i - 1]) if i else base
            after = side(classes[end]) if end < len(classes) else base
            settled = before if before == after else base
            for j in range(i, end):
                classes[j] = settled
            i = end
        else:
            i += 1
    return classes


def _levels(classes: list[str], base_level: int) -> list[int]:
    """How deeply nested each character is. Odd means it is drawn right to left."""
    out: list[int] = []
    for kind in classes:
        if base_level % 2 == 0:                     # a left-to-right line
            if kind == "R":
                out.append(base_level + 1)
            elif kind in ("EN", "AN"):
                out.append(base_level + 2)
            else:
                out.append(base_level)
        else:                                       # a right-to-left line
            out.append(base_level + 1 if kind in ("L", "EN", "AN") else base_level)
    return out


def reorder(text: str) -> str:
    """Put the characters in the order a left-to-right renderer should draw them.

    A right-to-left line comes out reversed, with the Latin words and the
    numbers inside it turned back the right way round; a left-to-right line
    with a name in Arabic or Hebrew in it gets that name reversed and nothing
    else touched.
    """
    if not is_rtl(text):
        return text

    # The line's own direction is set by its first real letter, before any of
    # the weak or neutral rules have had a chance to call something a letter
    # that is not one.
    first = next((k for k in map(_initial_class, text) if k in ("L", "R", "AL")), "L")
    base_level = 0 if first == "L" else 1
    classes = _resolve(text, "R" if base_level else "L")
    levels = _levels(classes, base_level)

    chars = [_MIRRORED.get(ch, ch) if levels[i] % 2 else ch
             for i, ch in enumerate(text)]

    # Reverse the deepest stretches first, then the ones they sit inside. A
    # left-to-right run nested in a right-to-left line is reversed twice and so
    # comes out in its own order, which is the point of doing it this way.
    odd = [level for level in levels if level % 2]
    if not odd:
        return "".join(chars)
    for level in range(max(levels), min(odd) - 1, -1):
        at = 0
        while at < len(chars):
            if levels[at] >= level:
                end = at
                while end < len(chars) and levels[end] >= level:
                    end += 1
                chars[at:end] = chars[at:end][::-1]
                levels[at:end] = levels[at:end][::-1]
                at = end
            else:
                at += 1
    return "".join(chars)


def shape(text: str, can_draw=None) -> str:
    """Join the letters and put them in printing order. Safe on any string."""
    if not text or not is_rtl(text):
        return text
    return reorder(join(text, can_draw))
