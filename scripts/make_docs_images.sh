#!/usr/bin/env bash
# Regenerate every terminal image in the README from a real carve run, so the
# screenshots can never drift from what the tool actually prints.
#
#   ./scripts/make_docs_images.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

demo="$work/demo"
mkdir -p "$demo/src" "$demo/tests" "$demo/docs" "$demo/config"

python3 - "$demo" <<'PY'
import os
import sys

root = sys.argv[1]
for i in range(1, 13):
    with open(os.path.join(root, "src", "module_%d.py" % i), "w") as fh:
        fh.write('''"""Noise module %d."""

CONSTANT_%d = %d


def helper_%d(value):
    """Does something irrelevant."""
    total = 0
    for step in range(value):
        total += step * CONSTANT_%d
    return total


class Thing%d:
    def __init__(self, name):
        self.name = name
        self.count = 0

    def bump(self):
        self.count += 1
        return self.count
''' % (i, i, i, i, i, i))

for i in range(1, 6):
    with open(os.path.join(root, "docs", "note_%d.md" % i), "w") as fh:
        fh.write("# Doc %d\n\nSome prose.\n" % i)
    with open(os.path.join(root, "config", "conf_%d.ini" % i), "w") as fh:
        fh.write("[s]\nkey_%d = value_%d\n" % (i, i))

with open(os.path.join(root, "src", "parser.py"), "w") as fh:
    fh.write('''"""The module that actually matters."""

import re

TOKEN = re.compile(r"[a-z]+")
LIMIT = 10


def preprocess(text):
    """Strip and lower, irrelevant to the bug."""
    return text.strip().lower()


def tokenize(text):
    return TOKEN.findall(preprocess(text))


def summarise(text):
    tokens = tokenize(text)
    # The bug: an off-by-one when the text is empty.
    return tokens[len(tokens) - 1]


def pretty(text):
    return "<" + summarise(text) + ">"
''')

with open(os.path.join(root, "tests", "test_parser.py"), "w") as fh:
    fh.write('''import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import parser


def test_summarise_words():
    assert parser.summarise("hello world") == "world"


def test_summarise_empty():
    assert parser.summarise("") == ""
''')
PY

mkdir -p "$here/docs/images"

FORCE_COLOR=1 python3 -m carve "$demo" -o "$work/out" --force -q -j 4 \
  -- python3 -m pytest tests -q > "$work/reduce.ansi" 2>&1 || true
sed "s|$work|~|g" "$work/reduce.ansi" \
  | python3 "$here/scripts/ansi_to_svg.py" "$here/docs/images/carved.svg" \
      --title "carve -- pytest tests -q"

FORCE_COLOR=1 python3 -m carve check "$demo" \
  -- python3 -m pytest tests -q > "$work/check.ansi" 2>&1 || true
sed "s|$work|~|g" "$work/check.ansi" | head -8 \
  | python3 "$here/scripts/ansi_to_svg.py" "$here/docs/images/check.svg" \
      --title "carve check -- pytest tests -q"

echo "wrote docs/images/carved.svg and docs/images/check.svg"
