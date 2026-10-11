#!/usr/bin/env python3
"""Compile the widget's QML files with PyQt6's QML engine and report errors.

  check_qml.py FILE.qml...            syntax/compile errors fail; missing Plasma modules
                                      are tolerated (CI runners have no Plasma)
  check_qml.py --strict FILE.qml...   every error fails (run on a Plasma 6 desktop: the
                                      system PyQt6 then resolves all org.kde.* imports)

QML is parsed before its imports are resolved, so a syntax error is reported even
where the Plasma modules are missing.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtQml import QQmlComponent, QQmlEngine  # noqa: E402

MISSING_MODULE = ("is not installed", "module not found")


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    files = [a for a in argv if a != "--strict"]
    if not files:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    app = QGuiApplication(sys.argv[:1])  # noqa: F841  (QML needs an application object)
    engine = QQmlEngine()
    failed = 0
    for path in files:
        component = QQmlComponent(engine, QUrl.fromLocalFile(os.path.abspath(path)))
        errors = [e.toString() for e in component.errors()]
        fatal = errors if strict else [e for e in errors if not any(m in e for m in MISSING_MODULE)]
        if fatal:
            failed += 1
            print(f"FAIL {path}")
            for e in fatal:
                print(f"  {e}")
        else:
            note = f" ({len(errors)} missing-module error(s) ignored)" if errors else ""
            print(f"ok   {path}{note}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
