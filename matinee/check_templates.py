"""Two ways a Jinja template silently throws your work away.

    python -m matinee.check_templates

A SCRIPT IN THE WRONG BLOCK. recommend.html opened its <script> inside
{% block title %}, and base.html renders that block inside <title>. The result
is not a broken script, it is not a script at all: it is the text of the
browser tab. The party buttons on the Recommend screen never had a handler, and
the tab read "Matinee <script> /* Picking a party ticks its people...".

A BLOCK THE PARENT DOES NOT RENDER. A child may define any block it likes; if
the parent never yields it, the content is dropped without a word. That is the
same failure wearing a different hat, and it is the one you hit while fixing
the first.

Neither raises, neither logs, and both look exactly like a feature nobody wired
up yet.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "templates")

BLOCK = re.compile(r"{%-?\s*block\s+(\w+)")
EXTENDS = re.compile(r"""{%-?\s*extends\s+["']([^"']+)["']""")

# Blocks whose content lands somewhere that cannot run or show markup.
TEXT_ONLY = {"title"}


def read(name: str) -> str:
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def body_of(text: str, block: str) -> str:
    """The source between a named block and its matching endblock."""
    m = re.search(r"\{%-?\s*block\s+" + re.escape(block) + r"\s*-?%\}", text)
    if not m:
        return ""
    depth, i = 1, m.end()
    for tok in re.finditer(r"{%-?\s*(block|endblock)\b", text[m.end():]):
        depth += 1 if tok.group(1) == "block" else -1
        if depth == 0:
            i = m.end() + tok.start()
            break
    return text[m.end():i]


def main() -> int:
    bad = 0
    names = sorted(f for f in os.listdir(TEMPLATES) if f.endswith(".html"))

    for name in names:
        text = read(name)

        # 1. Nothing that needs to run or be drawn may sit in a text-only block.
        for block in TEXT_ONLY:
            inner = body_of(text, block)
            for tag in ("<script", "<style", "<div", "<button"):
                if tag in inner:
                    print("  FAIL  %s: %s inside {%% block %s %%}, which renders "
                          "as text" % (name, tag, block))
                    bad += 1

        # 2. Every block a child defines must be one the parent yields.
        parent = EXTENDS.search(text)
        if not parent:
            continue
        try:
            offered = set(BLOCK.findall(read(parent.group(1))))
        except OSError:
            print("  FAIL  %s extends %s, which is missing" % (name, parent.group(1)))
            bad += 1
            continue
        for block in set(BLOCK.findall(text)):
            if block not in offered:
                print("  FAIL  %s defines {%% block %s %%}, which %s never renders"
                      % (name, block, parent.group(1)))
                bad += 1

    print("  %s  %d template(s)" % ("FAILED" if bad else "ok", len(names)))
    return bad


if __name__ == "__main__":
    sys.exit(main())
