# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The append-only result log (JSONL) and the export helpers.

One JSON object per line under the data directory (default
/data/forgetest/results.jsonl, override FORGETEST_DATA). Records:

  campaign    {"id","manifest_sha","catalog_hash","image"}   a campaign opened
  result      {"campaign","test","result","fingerprint","manifest_sha",
               "image","duration_s","evidence","answers","log","message"}
  invalidate  {"reason"}       manual invalidate-all (full campaign required)
  reset       {"reason"}       explicit campaign reset
  disposition {"test","fingerprint","reason"}   the operator's answer to a
               FAIL or ERROR that blocks its fingerprint (campaign.py)
  export      {"artifact_sha256","authorized","campaign"}

Every record carries "t" (type), "ts" (UTC, ISO 8601, seconds), "seq"
(its place in the file, from 1) and "boot" (the kernel's boot id when it
was written). The board has no battery-backed clock, so "ts" can run
behind the order of the file until the clock is set; "seq" is what an
order is judged by. A record written before records carried a "seq" is
given its place in the file when it is read. The file is only ever
appended; a corrupt line is skipped, counted, and reported, never
repaired in place.

Because the file only grows, `read` parses each line once and keeps what
it parsed; a later call reads only the bytes appended since. A result
record carries the whole run log, so the file reaches megabytes over a
campaign, and the page asks for the state every second or two.
"""
import json
import os
import threading
import time

DEFAULT_DATA_DIR = "/data/forgetest"


def now_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def data_dir():
    return os.environ.get("FORGETEST_DATA") or DEFAULT_DATA_DIR


BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def boot_id(path=BOOT_ID_PATH):
    """The kernel's id of this boot, or None where there is none."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


class Log:
    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "results.jsonl")
        self._lock = threading.Lock()
        self.corrupt = 0
        self._recs = []       # every record parsed so far, in file order
        self._offset = 0      # bytes of the file already consumed
        self._tail = b""      # bytes past the last newline: not a record yet
        self._boot = boot_id()

    def append(self, rec):
        rec = dict(rec)
        rec.setdefault("ts", now_ts())
        rec["boot"] = self._boot
        with self._lock:
            self._refresh()
            if self._tail:
                # A line a power cut left unfinished is ended first and read
                # again: a whole record that lost only its newline takes its
                # place (anything else is counted corrupt), and this record
                # stands on a line of its own after it.
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                self._refresh()
            # The record's place is counted from what the file holds now.
            last = self._recs[-1]["seq"] if self._recs else 0
            rec["seq"] = max(last, len(self._recs)) + 1
            line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return rec

    def _forget(self):
        """Drop what was parsed: the next read starts from the top."""
        self._recs = []
        self._offset = 0
        self._tail = b""
        self.corrupt = 0

    def _consume(self, chunk):
        """Parse the whole lines in `chunk`, holding back a partial tail."""
        data = self._tail + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self._tail = data
            return
        self._tail = data[cut + 1:]
        for line in data[:cut].split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.corrupt += 1
                continue
            if isinstance(rec, dict) and "t" in rec:
                if not isinstance(rec.get("seq"), int) or isinstance(rec.get("seq"), bool):
                    rec["seq"] = len(self._recs) + 1
                self._recs.append(rec)
            else:
                self.corrupt += 1

    def _refresh(self):
        """Parse what was appended since the last call (the lock held); a
        file that shrank or was replaced is read again from the top."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            self._forget()
            return
        if size < self._offset:
            self._forget()
        if size != self._offset:
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
            self._offset += len(chunk)
            self._consume(chunk)

    def read(self):
        """Every record, in file order. Only the bytes appended since the
        last call are parsed; a file that shrank or was replaced is read
        again from the top."""
        with self._lock:
            self._refresh()
            return list(self._recs)

    def raw(self):
        if not os.path.exists(self.path):
            return ""
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as f:
                return f.read()
