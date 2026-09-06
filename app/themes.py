"""The colours the interface is drawn in.

Somebody who looks at accounting software for six hours a day should get to
decide what it looks like. So every colour in the stylesheet comes from a named
token, and a theme is nothing more than a different set of values for those
tokens — which means adding one is a few lines here and no changes anywhere
else.

The tokens are named for what they *do* rather than what colour they are.
A variable called ``--green`` in a blue theme is a trap somebody falls into six
months later; ``--accent`` never lies.

Each person chooses their own. A company-wide default exists too, so an office
can be given a house look, but it is only a default — nobody is stuck with
somebody else's taste, and two people on the same network can see the same
books in different colours.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LIGHT, DARK = "light", "dark"


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    note: str
    mode: str = LIGHT
    tokens: dict[str, str] = field(default_factory=dict)

    @property
    def is_dark(self) -> bool:
        return self.mode == DARK

    def css(self) -> str:
        """This theme as a CSS rule, ready to drop into the stylesheet."""
        body = " ".join(f"--{k}: {v};" for k, v in self.tokens.items())
        return f'[data-theme="{self.key}"] {{ {body} }}'

    @property
    def swatch(self) -> tuple[str, str, str]:
        """Three colours that show what it looks like, for the picker."""
        return (self.tokens.get("side-bg", "#084d2a"),
                self.tokens.get("accent", "#0b6b3a"),
                self.tokens.get("bg", "#f4f6f8"))


def _light(accent, accent_dark, accent_soft, accent_line, side_bg=None,
           side_text=None, side_muted=None, side_active=None, **over):
    """A light theme: paper background, dark text, coloured chrome."""
    tokens = {
        "accent": accent,
        "accent-dark": accent_dark,
        "accent-soft": accent_soft,
        "accent-line": accent_line,
        "side-bg": side_bg or accent_dark,
        "side-text": side_text or "#dfe8e4",
        "side-muted": side_muted or "#9fb3ac",
        "side-active": side_active or accent_soft,
        "ink": "#16202b",
        "ink-soft": "#5a6672",
        "line": "#dde3e8",
        "bg": "#f4f6f8",
        "card": "#ffffff",
        "card-soft": "#fafbfc",
        # "good" is not the accent. A figure that helped profit must read as
        # good in every theme, including the ones whose accent is red or blue,
        # so it gets a token of its own rather than borrowing the brand colour.
        "good": "#0b6b3a",
        "danger": "#b3261e",
        "danger-bg": "#fdecea",
        "danger-line": "#f3c4c0",
        "warn": "#8a5a00",
        "warn-bg": "#fff5e0",
        "warn-line": "#f0dcb0",
        "info": "#0b4f8a",
        "info-bg": "#e8f1fa",
        "info-line": "#c3ddf3",
    }
    tokens.update(over)
    return tokens


def _dark(accent, accent_dark, accent_soft, side_bg, **over):
    """A dark theme. Not an inverted light one — the greys are chosen so that
    a dense table of figures stays readable rather than glowing."""
    tokens = {
        "accent": accent,
        "accent-dark": accent_dark,
        "accent-soft": accent_soft,
        "accent-line": accent_dark,
        "side-bg": side_bg,
        "side-text": "#c6cfd8",
        "side-muted": "#7c8794",
        "side-active": accent,
        "ink": "#e6ebf0",
        "ink-soft": "#9aa5b1",
        "line": "#2b3441",
        "bg": "#161c24",
        "card": "#1e2630",
        "card-soft": "#232c37",
        "good": "#6fcf97",
        "danger": "#ff8a80",
        "danger-bg": "#3a2220",
        "danger-line": "#5c322e",
        "warn": "#ffcc7a",
        "warn-bg": "#3a2f1c",
        "warn-line": "#5a4826",
        "info": "#8ec5ff",
        "info-bg": "#1c2a3a",
        "info-line": "#2c4258",
    }
    tokens.update(over)
    return tokens


THEMES: list[Theme] = [
    Theme("ledger", "Ledger Green", "The original. Quiet and bookish.",
          LIGHT, _light("#0b6b3a", "#084d2a", "#e7f3ec", "#b9dcc8",
                        side_text="#cfe4d8", side_muted="#7fae95",
                        side_active="#6fcf97")),
    Theme("ocean", "Ocean Blue", "What most people picture when they think of accounts.",
          LIGHT, _light("#0d5ea6", "#0a4276", "#e8f1fb", "#bcd8f2",
                        side_text="#cfdff0", side_muted="#7fa3c6",
                        side_active="#6fb4f0")),
    Theme("indigo", "Indigo", "Cooler and more modern.",
          LIGHT, _light("#4338ca", "#312a92", "#eeecfb", "#c9c4f0",
                        side_text="#d6d3f2", side_muted="#9a95c8",
                        side_active="#a5a0f5")),
    Theme("slate", "Slate", "Almost no colour at all. Lets the figures speak.",
          LIGHT, _light("#334155", "#1e293b", "#eef1f5", "#cbd5e1",
                        side_text="#cbd5e1", side_muted="#8b97a6",
                        side_active="#94a3b8")),
    Theme("teal", "Teal", "Fresh without being loud.",
          LIGHT, _light("#0d7377", "#08514f", "#e4f4f4", "#b3dedd",
                        side_text="#cbe6e5", side_muted="#7fb0af",
                        side_active="#5fd0cc")),
    Theme("burgundy", "Burgundy", "Warm and formal.",
          LIGHT, _light("#8c1d3f", "#63132c", "#fbeaef", "#eec3d1",
                        side_text="#eccdd6", side_muted="#b98b99",
                        side_active="#e58aa0")),
    Theme("bronze", "Bronze", "Earthy. Easy on the eyes in a bright room.",
          LIGHT, _light("#8a5a1a", "#5e3c0e", "#faf0e2", "#e8d2ae",
                        side_text="#ecdcc4", side_muted="#b89a72",
                        side_active="#e0a94f")),
    Theme("plum", "Plum", "Distinctive, and still calm.",
          LIGHT, _light("#6b2d8c", "#4a1d63", "#f4eafa", "#dcc3ec",
                        side_text="#e0cdec", side_muted="#a88bb8",
                        side_active="#c58ae0")),
    Theme("midnight", "Midnight", "Dark, with a blue accent. For long evenings.",
          DARK, _dark("#5aa9f0", "#2f6ea8", "#1d2c3c", "#111820")),
    Theme("carbon", "Carbon", "Dark and almost colourless.",
          DARK, _dark("#8fa3b8", "#5c6f84", "#242c36", "#12161b")),
    Theme("contrast", "High Contrast", "Heavier text and stronger lines, for tired eyes.",
          LIGHT, _light("#00408a", "#002c5f", "#e3edf9", "#7fa8d4",
                        side_text="#ffffff", side_muted="#c3d4e6",
                        side_active="#ffd24d",
                        ink="#000000", **{"good": "#00521f",
                                          "ink-soft": "#33404d",
                                          "line": "#9aa8b5",
                                          "bg": "#ffffff",
                                          "card": "#ffffff",
                                          "card-soft": "#f2f5f8"})),
]

BY_KEY = {t.key: t for t in THEMES}
DEFAULT = "ledger"


def get(key: str | None) -> Theme:
    return BY_KEY.get((key or "").strip().lower(), BY_KEY[DEFAULT])


def resolve(user=None, company=None) -> str:
    """Whose taste wins: the person's, then the company's, then the default."""
    for candidate in (getattr(user, "theme", ""), getattr(company, "theme", "")):
        if candidate and candidate in BY_KEY:
            return candidate
    return DEFAULT


def stylesheet() -> str:
    """Every theme, as CSS. Appended to app.css when it is served."""
    lines = ["", "/* ---- Themes. Generated from app/themes.py ---- */"]
    lines += [t.css() for t in THEMES]
    return "\n".join(lines) + "\n"


def light() -> list[Theme]:
    return [t for t in THEMES if not t.is_dark]


def dark() -> list[Theme]:
    return [t for t in THEMES if t.is_dark]


def rgb(value: str) -> tuple[float, float, float]:
    """A #rrggbb string as the 0-to-1 triple a PDF wants."""
    text = (value or "").lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return (0.0, 0.0, 0.0)
    try:
        return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore
    except ValueError:
        return (0.0, 0.0, 0.0)


def accent_rgb(company=None) -> tuple[float, float, float]:
    """The company's own accent colour, for documents it sends out.

    Documents follow the *company's* theme, never the individual's: an invoice
    is the business writing to its customer, and it should not change colour
    depending on which member of staff happened to press the button.
    """
    theme = get(getattr(company, "theme", None))
    return rgb(theme.tokens.get("accent-dark") or theme.tokens.get("accent"))
