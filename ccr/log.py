"""Console logging.

Deliberately plain: a long crawl is watched by a human, and timestamps plus
one line per event is what makes an unattended run reviewable afterwards.
"""

import datetime
import sys


class Logger:
    def __init__(self, stream=None, quiet=False):
        self.stream = stream or sys.stdout
        self.quiet = quiet

    def __call__(self, message):
        self.write(message)

    def write(self, message):
        if self.quiet:
            return
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.stream.write(f"[{stamp}] {message}\n")
        self.stream.flush()

    def rule(self, title=""):
        self.write("-" * 8 + (f" {title} " if title else "") + "-" * 8)
