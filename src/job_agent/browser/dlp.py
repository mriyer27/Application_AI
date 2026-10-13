"""Data-loss prevention for the request guard (SEC-1).

Once filling begins, ``session._Guard`` asks ``ProtectedData.match`` whether a
request carries something that was typed into the form or attached to it,
before letting it go anywhere -- the job's own host included. A request is
inspected in every form a page script could plausibly use:

* the raw URL (and body), percent-decoded up to three times (``%xx`` and ``+``);
* ``\\uXXXX`` / ``%uXXXX`` escapes;
* base64 / base64url runs (every alignment) and hex runs, decoded;
* raw bytes (``unquote_to_bytes``) and latin-1 "binary strings".

Typed values are matched case-insensitively once they have at least
``MIN_VALUE_CHARS`` characters (short ones only on token boundaries, so
``"Test"`` does not match ``latest.js``); long values are also matched by any
``TEXT_WINDOW``-character fragment, phone numbers by their digits, e-mail
addresses by their local part. Uploaded files are indexed as ``FILE_WINDOW``-byte
windows, so any contiguous chunk of about 30 bytes or more is recognised.

This is a content check and therefore heuristic: a script that transforms the
data first (reverses it, sends character codes, splits it into fragments of
one to three characters) is not recognised here. The guard closes those
channels structurally instead (no navigations, no new URLs with a query
string, no new non-asset requests, a small budget of new asset URLs after
filling began); see ``session._Guard``.
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import unquote, unquote_plus, unquote_to_bytes

#: Shortest typed value that is protected (shorter ones are too generic: "Yes", "No", "NY").
MIN_VALUE_CHARS = 4
#: Values shorter than this only match on token boundaries.
_BOUNDARY_BELOW = 8
#: Fragment length for long typed values (any 27+ character fragment is recognised).
TEXT_WINDOW = 24
_TEXT_STEP = 4
#: Window length for uploaded files (any contiguous ~31-byte chunk is recognised).
FILE_WINDOW = 24
_FILE_STEP = 8
_MAX_FILE_WINDOWS = 300_000
#: Only this much of a URL / body is inspected (longer requests are refused by the guard's structural rules).
_MAX_INSPECT = 256 * 1024

#: Checkbox words: what the agent types into yes/no controls, not personal data.
_GENERIC = frozenset({"true", "false", "checked", "check", "unchecked", "uncheck", "agree", "i agree", "none",
                      "null", "undefined"})

_SEP = re.compile(r"[\s\-._()+/,:;]+")
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")
_HEX_RUN = re.compile(r"[0-9a-fA-F]{16,}")
_UESC = re.compile(r"(?:\\u|%u)([0-9a-fA-F]{4})")


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


class ProtectedData:
    """Everything typed into / attached to one application page."""

    def __init__(self) -> None:
        self._values: dict[str, None] = {}
        self._compact: dict[str, None] = {}
        self._text_windows: set[str] = set()
        self._file_windows: set[bytes] = set()

    def __bool__(self) -> bool:
        return bool(self._values or self._compact or self._file_windows)

    # -- registration ------------------------------------------------------ #
    def add_value(self, value: str | None) -> None:
        v = _norm(value or "")
        if len(v) < MIN_VALUE_CHARS or v in _GENERIC:
            return
        self._values[v] = None
        compact = _SEP.sub("", v)
        if len(compact) >= 7 and compact != v:
            self._compact[compact] = None
        digits = re.sub(r"\D", "", v)
        if len(digits) >= 7 and len(digits) >= len(compact) - 3:  # a phone number: match its digits
            self._compact[digits] = None
            if len(digits) == 11 and digits.startswith("1"):
                self._compact[digits[1:]] = None
        if "@" in v:
            local = v.split("@", 1)[0]
            if len(local) >= MIN_VALUE_CHARS:
                self._values[local] = None
        if len(v) > TEXT_WINDOW + _TEXT_STEP:
            for i in range(0, len(v) - TEXT_WINDOW + 1, _TEXT_STEP):
                self._text_windows.add(v[i:i + TEXT_WINDOW])

    def add_file(self, data: bytes, name: str | None = None) -> None:
        if name:
            self.add_value(name)
        if len(data) < FILE_WINDOW:
            self.add_value(data.decode("latin-1"))
            return
        step = max(_FILE_STEP, len(data) // _MAX_FILE_WINDOWS + 1)
        for i in range(0, len(data) - FILE_WINDOW + 1, step):
            window = data[i:i + FILE_WINDOW]
            if len(set(window)) >= 5:  # skip padding / runs of one byte
                self._file_windows.add(window)

    # -- matching ---------------------------------------------------------- #
    def match(self, url: str, body: bytes | None = None) -> str | None:
        """What protected data ``url`` / ``body`` carries ("typed data" / "uploaded file content"), or None."""
        if not self:
            return None
        texts, blobs = _decodings(url[:_MAX_INSPECT], (body or b"")[:_MAX_INSPECT])
        if self._values or self._compact:
            for text in texts:
                if self._text_hit(text.casefold()):
                    return "typed data"
        if self._file_windows:
            for blob in blobs:
                if self._blob_hit(blob):
                    return "uploaded file content"
        return None

    def _text_hit(self, text: str) -> bool:
        for v in self._values:
            start = text.find(v)
            while start >= 0:
                end = start + len(v)
                if len(v) >= _BOUNDARY_BELOW or (_edge(text, start - 1) and _edge(text, end)):
                    return True
                start = text.find(v, start + 1)
        if self._compact:
            compact = _SEP.sub("", text)
            if any(c in compact for c in self._compact):
                return True
        if self._text_windows and len(text) >= TEXT_WINDOW:
            windows = self._text_windows
            return any(text[i:i + TEXT_WINDOW] in windows for i in range(len(text) - TEXT_WINDOW + 1))
        return False

    def _blob_hit(self, blob: bytes) -> bool:
        if len(blob) < FILE_WINDOW:
            return False
        windows = self._file_windows
        return any(blob[i:i + FILE_WINDOW] in windows for i in range(len(blob) - FILE_WINDOW + 1))


def _edge(text: str, i: int) -> bool:
    return i < 0 or i >= len(text) or not text[i].isalnum()


def _decodings(url: str, body: bytes) -> tuple[list[str], list[bytes]]:
    """Plain-text and raw-byte readings of a request (see the module docstring)."""
    texts: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            texts.append(text)

    def layers(text: str) -> None:
        for _ in range(3):
            add(text)
            add(unquote_plus(text))
            decoded = unquote(text)
            if decoded == text:
                break
            text = decoded
        add(text)

    layers(url)
    blobs: list[bytes] = [unquote_to_bytes(url)]
    if body:
        blobs.append(body)
        layers(body.decode("utf-8", "ignore"))
    for text in list(texts):
        if "\\u" in text or "%u" in text:
            add(_UESC.sub(lambda m: chr(int(m.group(1), 16)), text))
    for text in list(texts):
        blobs.append(text.encode("latin-1", "ignore"))  # JS "binary strings" (FileReader.readAsBinaryString)
        for run in _B64_RUN.findall(text):
            for raw in _b64_variants(run):
                blobs.append(raw)
                decoded = raw.decode("utf-8", "ignore")
                add(decoded)
                add(unquote(decoded))
                add(raw.decode("latin-1"))
        for run in _HEX_RUN.findall(text):
            for off in (0, 1):
                part = run[off:]
                part = part[: len(part) - len(part) % 2]
                try:
                    raw = bytes.fromhex(part)
                except ValueError:
                    continue
                blobs.append(raw)
                add(raw.decode("utf-8", "ignore"))
    return texts, blobs


def _b64_variants(run: str) -> list[bytes]:
    s = run.rstrip("=").replace("-", "+").replace("_", "/")
    out: list[bytes] = []
    for off in range(4):
        part = s[off:]
        rem = len(part) % 4
        if rem == 1:
            part = part[:-1]
        elif rem:
            part += "=" * (4 - rem)
        if len(part) < 8:
            continue
        try:
            out.append(base64.b64decode(part, validate=True))
        except (binascii.Error, ValueError):
            continue
    return out


__all__ = ["FILE_WINDOW", "MIN_VALUE_CHARS", "TEXT_WINDOW", "ProtectedData"]
