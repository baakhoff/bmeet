#!/usr/bin/env python3
"""Checks BMeet.app's Russian texts against the texts in its source.

Every text BMeet.app shows is written in English in macos/Sources/BMeet/*.swift as L("…").
macos/app/ru.lproj/Localizable.strings has the Russian for each, with the English text as the
key. This checks that every text has a translation, that no translation is left over from a
text that is gone, that a translation has the same %@ and %d as its text and ends the same way
(":" or "…"), and that it has no «ё» (docs/macos.md, the words used). It also checks the two
tables macOS reads itself: ServicesMenu.strings (one title for each of Finder's services in
macos/app/Info.plist) and InfoPlist.strings (only keys that Info.plist has). Stdlib only, so it
runs on Linux too (CI's lint job).

  python3 macos/tools/check-strings.py          check; exits with 1 if something is wrong
  python3 macos/tools/check-strings.py --keys   print the texts in the source, one per line
"""
from __future__ import annotations

import plistlib
import re
import sys
from pathlib import Path

MACOS = Path(__file__).resolve().parents[1]
SOURCES = MACOS / "Sources/BMeet"
APP = MACOS / "app"
CALL = re.compile(r'\bL\(\s*"((?:[^"\\]|\\.)*)"')
PLACEHOLDER = re.compile(r"%(?:\d+\$)?(@|ld|d)")
SWIFT_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "'": "'", "0": "\0"}


def texts_in_source() -> dict[str, str]:
    """{English text: where it first is}."""
    found: dict[str, str] = {}
    for path in sorted(SOURCES.glob("*.swift")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("//"):   # (a comment may show how L("…") is written)
                continue
            for raw in CALL.findall(line):
                if "\\(" in raw:
                    sys.exit(f'{path.name}:{number}: L("…") with \\( ) in it: use %@ and pass the value.')
                text = re.sub(r"\\(.)", lambda m: SWIFT_ESCAPES.get(m.group(1), m.group(0)), raw)
                found.setdefault(text, f"{path.name}:{number}")
    return found


class StringsError(Exception):
    pass


def parse_strings(text: str) -> dict[str, str]:
    """A .strings file in the text format macOS reads: "key" = "value"; with /* */ and //
    comments, the usual escapes, and keys without quotes (InfoPlist.strings)."""
    pos, n, out = 0, len(text), {}

    def skip() -> None:
        nonlocal pos
        while pos < n:
            if text[pos].isspace():
                pos += 1
            elif text.startswith("/*", pos):
                end = text.find("*/", pos + 2)
                if end < 0:
                    raise StringsError("a /* comment that never ends")
                pos = end + 2
            elif text.startswith("//", pos):
                end = text.find("\n", pos)
                pos = n if end < 0 else end + 1
            else:
                return

    def token() -> str:
        nonlocal pos
        skip()
        if pos >= n:
            raise StringsError("the file ends in the middle of an entry")
        if text[pos] == '"':
            pos += 1
            chars = []
            while pos < n and text[pos] != '"':
                if text[pos] == "\\":
                    pos += 1
                    c = text[pos] if pos < n else ""
                    if c in ("U", "u"):
                        chars.append(chr(int(text[pos + 1:pos + 5], 16)))
                        pos += 5
                        continue
                    chars.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(c, c))
                else:
                    chars.append(text[pos])
                pos += 1
            if pos >= n:
                raise StringsError("a string that never ends")
            pos += 1
            return "".join(chars)
        m = re.compile(r"[A-Za-z0-9_.$-]+").match(text, pos)
        if not m:
            raise StringsError(f"unexpected {text[pos]!r} at line {text.count(chr(10), 0, pos) + 1}")
        pos = m.end()
        return m.group(0)

    def expect(mark: str) -> None:
        nonlocal pos
        skip()
        if not text.startswith(mark, pos):
            raise StringsError(f"expected {mark!r} at line {text.count(chr(10), 0, pos) + 1}")
        pos += 1

    while True:
        skip()
        if pos >= n:
            return out
        key = token()
        expect("=")
        value = token()
        expect(";")
        if key in out:
            raise StringsError(f"{key!r} twice")
        out[key] = value


def placeholders(text: str) -> list[str]:
    return sorted(PLACEHOLDER.findall(text))


def ending(text: str) -> str:
    return next((mark for mark in (":", "…") if text.endswith(mark)), "")


def load(path: Path, problems: list[str]) -> dict[str, str] | None:
    try:
        return parse_strings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, StringsError) as exc:
        problems.append(f"{path.relative_to(MACOS.parent)}: not a strings file: {exc}")
        return None


def main() -> int:
    source = texts_in_source()
    if "--keys" in sys.argv:
        print("\n".join(source))
        return 0
    problems: list[str] = []
    info = plistlib.loads((APP / "Info.plist").read_bytes())

    english = load(APP / "en.lproj/Localizable.strings", problems)
    if english:
        problems += [f"en.lproj/Localizable.strings: English is the text in the source, but this has: {k}" for k in english]
    russian = load(APP / "ru.lproj/Localizable.strings", problems)
    if russian is not None:
        for text, where in source.items():
            translation = russian.get(text)
            if translation is None:
                problems.append(f"ru.lproj/Localizable.strings: no translation for the text at {where}: {text}")
            elif not translation.strip():
                problems.append(f"ru.lproj/Localizable.strings: empty translation: {text}")
            elif placeholders(translation) != placeholders(text):
                problems.append(f"ru.lproj/Localizable.strings: other placeholders than its text: {text}")
            elif ending(translation) != ending(text):
                problems.append(f"ru.lproj/Localizable.strings: ends otherwise than its text: {text}")
        problems += [f"ru.lproj/Localizable.strings: a translation of a text that is not in the source: {k}"
                     for k in russian if k not in source]

    services = load(APP / "ru.lproj/ServicesMenu.strings", problems)
    if services is not None:
        titles = [s["NSMenuItem"]["default"] for s in info.get("NSServices", [])]
        problems += [f"ru.lproj/ServicesMenu.strings: no title for the service {t}" for t in titles if t not in services]
        problems += [f"ru.lproj/ServicesMenu.strings: {k} is not a service in Info.plist" for k in services if k not in titles]
    plist = load(APP / "ru.lproj/InfoPlist.strings", problems)
    if plist is not None:
        problems += [f"ru.lproj/InfoPlist.strings: {k} is not in Info.plist" for k in plist if k not in info]

    for name, table in (("Localizable", russian), ("ServicesMenu", services), ("InfoPlist", plist)):
        problems += [f"ru.lproj/{name}.strings: «ё» in: {v}" for v in (table or {}).values() if "ё" in v.lower()]
    if problems:
        print("\n".join(problems))
        return 1
    print(f"strings: ok ({len(source)} texts; Russian, Finder's services, Info.plist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
