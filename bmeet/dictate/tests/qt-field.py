#!/usr/bin/python3 -I
"""A text field for the tests (Qt): logs what the input method does to it (preedit, commit) and
its focus, as JSON lines with time.time(). Usage: qt-field.py LOG SECONDS"""
import json
import sys
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QLineEdit

out = open(sys.argv[1], "a", buffering=1)


def note(**kw):
    kw["t"] = time.time()
    out.write(json.dumps(kw, ensure_ascii=False) + "\n")


class Field(QLineEdit):
    def inputMethodEvent(self, e):
        note(ev="im", preedit=e.preeditString(), commit=e.commitString())
        super().inputMethodEvent(e)

    def focusInEvent(self, e):
        note(ev="focus-in")
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        note(ev="focus-out")
        super().focusOutEvent(e)


app = QApplication(sys.argv[:1])
field = Field()
field.resize(900, 40)
field.show()
note(ev="start", platform=app.platformName())
QTimer.singleShot(int(float(sys.argv[2]) * 1000), lambda: (note(ev="end", text=field.text()), app.quit()))
app.exec()
