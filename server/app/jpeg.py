"""Lossless JPEG metadata stripping.

Browser canvases do not write EXIF, but the server never trusts that: it
walks every marker up to the end of the image and keeps only what decoding
needs (JFIF APP0, Adobe APP14 colour info, tables, frame and scan headers,
entropy-coded data). Every other APPn segment (EXIF, XMP, MPF secondary
images...), comments and any bytes after the end-of-image marker are dropped
before a frame touches the disk. Image data is untouched.
"""

from __future__ import annotations

import re

KEEP_APP = {0xE0, 0xEE}  # JFIF, Adobe
SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
STANDALONE = {0x01, *range(0xD0, 0xD8)}  # TEM, RST0-7: no length field
_MARKER = re.compile(rb"\xff(?!\x00|[\xd0-\xd7]|\xff)")  # a marker inside entropy-coded data
_FILL = re.compile(rb"[^\xff]")


class BadJpeg(ValueError):
    pass


def strip_metadata(data: bytes) -> tuple[bytes, int, int]:
    """Return (stripped JPEG, width, height). Raises BadJpeg if it is not a
    complete, well-formed JPEG."""
    if data[:2] != b"\xff\xd8":
        raise BadJpeg("not a JPEG")
    out = bytearray(b"\xff\xd8")
    i, n = 2, len(data)
    size = None
    while True:
        if i + 2 > n:
            raise BadJpeg("truncated JPEG")
        if data[i] != 0xFF:
            raise BadJpeg("corrupt JPEG marker")
        m = _FILL.search(data, i)  # skip fill bytes in one step
        if m is None:
            raise BadJpeg("truncated JPEG")
        i = m.start() - 1
        marker = data[i + 1]
        if marker == 0xD9:  # end of image: anything after it is dropped
            if size is None:
                raise BadJpeg("JPEG has no image data")
            out += b"\xff\xd9"
            return bytes(out), size[0], size[1]
        if marker in STANDALONE or marker in (0x00, 0xD8):
            raise BadJpeg("corrupt JPEG marker")
        if i + 4 > n:
            raise BadJpeg("truncated JPEG")
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > n:
            raise BadJpeg("truncated JPEG")
        seg = data[i:i + 2 + length]
        if marker in SOF:
            if length < 8:
                raise BadJpeg("corrupt JPEG frame header")
            h, w = int.from_bytes(seg[5:7], "big"), int.from_bytes(seg[7:9], "big")
            if size is not None or not (w and h):
                raise BadJpeg("unsupported JPEG frame header")
            size = (w, h)
        is_app = 0xE0 <= marker <= 0xEF
        if not ((is_app and marker not in KEEP_APP) or marker == 0xFE):
            out += seg
        i += 2 + length
        if marker == 0xDA:  # start of scan: copy entropy-coded data up to the next marker
            if size is None:
                raise BadJpeg("JPEG scan before frame header")
            m = _MARKER.search(data, i)
            if m is None:
                raise BadJpeg("truncated JPEG")
            out += data[i:m.start()]
            i = m.start()
