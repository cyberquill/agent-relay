"""The two tiny files scenario 4 sends, and the checks scenario 5 runs on `answer.html`."""

from __future__ import annotations

import re
import struct
import zlib
from html import unescape
from pathlib import Path

PNG_NAME = "relay-e2e-image.png"
PDF_NAME = "relay-e2e-doc.pdf"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def make_png(size: int = 8) -> bytes:
    """A valid `size` x `size` RGB image, solid colour."""
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes((200, 40, 40)) * size
    pixels = zlib.compress(row * size)
    return _PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", pixels) + _chunk(b"IEND", b"")


def make_pdf(text: str = "agent-relay e2e") -> bytes:
    """A valid one-page PDF with `text` on it (ASCII only)."""
    safe = text.encode("ascii", "replace").decode("ascii").replace("\\", "").replace("(", "").replace(")", "")
    stream = f"BT /F1 18 Tf 36 96 Td ({safe}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 160] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_samples(directory: Path) -> tuple[Path, Path]:
    """Write both files under `directory` (a temporary folder) and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    png, pdf = directory / PNG_NAME, directory / PDF_NAME
    png.write_bytes(make_png())
    pdf.write_bytes(make_pdf())
    return png, pdf


_CSP_META = re.compile(r"""<meta[^>]+http-equiv\s*=\s*["']?Content-Security-Policy["']?[^>]*>""", re.I | re.S)
_SCRIPT = re.compile(r"<\s*script\b", re.I)
_HREF = re.compile(r"\bhref\s*=", re.I)
_EXTERNAL = re.compile(r"""(?:\bsrc\s*=\s*["']?\s*https?:|url\(\s*["']?\s*https?:|@import)""", re.I)


def html_problems(data: bytes) -> list[str]:
    """Why `answer.html` is not the safe self-contained file the plan promises (empty: it is)."""
    html = data.decode("utf-8", "replace")
    problems: list[str] = []
    meta = _CSP_META.search(html)
    if meta is None:
        problems.append("no Content-Security-Policy meta tag")
    elif "default-src 'none'" not in unescape(meta.group(0)):
        problems.append("the CSP does not start from default-src 'none'")
    if _SCRIPT.search(html):
        problems.append("contains <script")
    if _HREF.search(html):
        problems.append("contains href=")
    if _EXTERNAL.search(html):
        problems.append("loads an external resource")
    return problems
