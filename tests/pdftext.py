"""Reading the text back out of a PDF the way a reader would.

A test that only looks for ``(text) Tj`` sees the Latin parts of a document and
nothing else, because text printed with an embedded font is written as glyph
numbers instead. This pulls those back through the document's own ToUnicode
tables — so a test that finds the words also proves the file can be searched
and copied from, which for an invoice matters as much as how it looks.
"""
from __future__ import annotations

import re
import zlib

_STREAM = re.compile(rb"stream\n(.*?)\nendstream", re.S)
_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_PAIR = re.compile(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]+)>")
_SHOWN = re.compile(rb"(?:\((.*?)\)|<([0-9A-Fa-f]*)>)\s*Tj", re.S)
_BLOCK = re.compile(rb"BT\n(.*?)\nET", re.S)


def _bodies(pdf: bytes) -> list[bytes]:
    out = []
    for match in _STREAM.finditer(pdf):
        try:
            out.append(zlib.decompress(match.group(1)))
        except zlib.error:
            continue
    return out


def _tables(bodies: list[bytes]) -> list[dict[int, str]]:
    """Every glyph-number-to-character table in the file."""
    tables = []
    for body in bodies:
        table: dict[int, str] = {}
        for block in _BFCHAR.findall(body):
            for cid, target in _PAIR.findall(block):
                table[int(cid, 16)] = bytes.fromhex(
                    target.decode()).decode("utf-16-be", "replace")
        if table:
            tables.append(table)
    return tables


def text_of(pdf: bytes) -> str:
    """Everything drawn on the page, one line per line of the document.

    A line printed in two fonts — an English word beside a Chinese name — is
    one text object in the file and comes back here as one line, which is what
    a test asking whether the customer's name is on the invoice needs.
    """
    bodies = _bodies(pdf)
    tables = _tables(bodies)
    lines: list[str] = []
    for body in bodies:
        if b"beginbfchar" in body:
            continue                      # that is a table, not a page
        for block in _BLOCK.findall(body):
            pieces: list[str] = []
            for match in _SHOWN.finditer(block):
                literal, glyphs = match.group(1), match.group(2)
                if literal is not None:
                    raw = (literal.replace(b"\\(", b"(")
                           .replace(b"\\)", b")").replace(b"\\\\", b"\\"))
                    pieces.append(raw.decode("cp1252", "replace"))
                    continue
                codes = [int(glyphs[i:i + 4], 16)
                         for i in range(0, len(glyphs), 4)]
                for table in tables:
                    if all(code in table for code in codes):
                        pieces.append("".join(table[code] for code in codes))
                        break
                else:
                    pieces.append("�" * len(codes))
            if pieces:
                lines.append("".join(pieces))
    return "\n".join(lines)
