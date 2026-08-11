#!/usr/bin/env python3
"""Render ANSI terminal output to a standalone SVG.

Used by ``make_docs_images.sh`` so every terminal screenshot in the README is a
real run of the tool rather than something typed by hand into an image editor.

    strata hotspots . | python3 scripts/ansi_to_svg.py out.svg --title "strata"
"""

from __future__ import annotations

import re
import sys

CELL_W = 8.4
LINE_H = 19.0
PAD_X = 18.0
PAD_TOP = 44.0
PAD_BOTTOM = 16.0

BG = "#0d1017"
CHROME = "#141922"
FG = "#e6ebf2"

# The 256-colour indices strata actually emits, plus a sane default.
XTERM = {
    60: "#5f5f87", 103: "#8787af", 110: "#87afd7", 179: "#d7af5f",
    203: "#ff5f5f", 209: "#ff875f", 214: "#ffaf00",
}

_SGR = re.compile(r"\033\[([0-9;]*)m")
_ESCAPES = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})


def parse(text):
    """Yield lines of ``(run_text, colour, dim, bold)`` spans."""
    for raw in text.rstrip("\n").split("\n"):
        spans, colour, dim, bold, cursor = [], FG, False, False, 0
        for match in _SGR.finditer(raw):
            chunk = raw[cursor:match.start()]
            if chunk:
                spans.append((chunk, colour, dim, bold))
            cursor = match.end()
            codes = [c for c in match.group(1).split(";") if c != ""] or ["0"]
            i = 0
            while i < len(codes):
                code = codes[i]
                if code == "0":
                    colour, dim, bold = FG, False, False
                elif code == "1":
                    bold = True
                elif code == "2":
                    dim = True
                elif code == "38" and i + 2 < len(codes) and codes[i + 1] == "5":
                    colour = XTERM.get(int(codes[i + 2]), FG)
                    i += 2
                i += 1
        tail = raw[cursor:]
        if tail:
            spans.append((tail, colour, dim, bold))
        yield spans


def render(text, title=""):
    lines = list(parse(text))
    columns = max((sum(len(s[0]) for s in line) for line in lines), default=40)
    width = PAD_X * 2 + columns * CELL_W
    height = PAD_TOP + len(lines) * LINE_H + PAD_BOTTOM

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="ui-monospace,SFMono-Regular,Menlo,'
        f'Consolas,monospace" font-size="13">',
        f'<rect width="{width:.0f}" height="{height:.0f}" rx="9" fill="{BG}"/>',
        f'<path d="M0 9a9 9 0 0 1 9-9h{width - 18:.0f}a9 9 0 0 1 9 9v22H0z" fill="{CHROME}"/>',
    ]
    for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        out.append(f'<circle cx="{20 + i * 17}" cy="16" r="5.5" fill="{colour}"/>')
    if title:
        out.append(
            f'<text x="{width / 2:.0f}" y="20" fill="#616d80" font-size="11.5" '
            f'text-anchor="middle">{title.translate(_ESCAPES)}</text>'
        )

    for row, spans in enumerate(lines):
        y = PAD_TOP + row * LINE_H + 12
        column = 0
        for chunk, colour, dim, bold in spans:
            if chunk.strip():
                x = PAD_X + column * CELL_W
                weight = ' font-weight="600"' if bold else ""
                opacity = ' opacity="0.62"' if dim else ""
                body = chunk.translate(_ESCAPES)
                out.append(
                    f'<text x="{x:.1f}" y="{y:.1f}" fill="{colour}"{weight}{opacity} '
                    f'xml:space="preserve">{body}</text>'
                )
            column += len(chunk)

    out.append("</svg>")
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ansi_to_svg.py OUT.svg [--title TEXT]", file=sys.stderr)
        return 2
    destination = sys.argv[1]
    title = ""
    if "--title" in sys.argv:
        title = sys.argv[sys.argv.index("--title") + 1]
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(render(sys.stdin.read(), title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
