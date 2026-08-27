"""Extract text (and optional image bytes) from non-plain files.

Leaf module: stdlib plus optional CLIs (pdftotext, whisper). tools.py and
the agent loop call this; it must not import agent, tools, loop, or graph.
"""

import base64
import io
import shutil
import struct
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_IMAGE_BYTES = 4_000_000
MAX_EXTRACT_CHARS = 200_000
WHISPER_TIMEOUT_S = 300
PDFTOTEXT_TIMEOUT_S = 60

KIND_TEXT = "text"
KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_XLSX = "xlsx"
KIND_PPTX = "pptx"
KIND_IMAGE = "image"
KIND_AUDIO = "audio"
KIND_ZIP = "zip"
KIND_BINARY = "binary"
# @path attachments inline these (text files stay path-only; images use vision).
INLINE_KINDS = frozenset({
    KIND_PDF, KIND_DOCX, KIND_XLSX, KIND_PPTX, KIND_AUDIO, KIND_ZIP,
})
EXCERPT_MAX_LINES = 400

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"})
PDF_SUFFIXES = frozenset({".pdf"})
OLE_SUFFIXES = frozenset({".doc", ".xls", ".ppt"})

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


@dataclass(frozen=True)
class MediaPart:
    """Image bytes the chat API can attach as an image_url part."""
    label: str
    mime: str
    data: bytes
    width: int = 0
    height: int = 0

    def caption(self) -> str:
        kb = max(1, (len(self.data) + 1023) // 1024)
        if self.width and self.height:
            extra = f"{self.width}x{self.height}, {kb}KB"
        else:
            extra = f"{kb}KB"
        return f"[{self.mime} {extra}] {self.label}"

    def content_part(self) -> dict:
        b64 = base64.b64encode(self.data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{self.mime};base64,{b64}"},
        }


@dataclass
class Extracted:
    """Result of reading a path or byte blob."""
    kind: str
    text: str
    media: "MediaPart | None" = None


def flatten_content(content: Any) -> str:
    """Plain text for logs/transcripts. Never includes base64 data URIs."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits = []
        for part in content:
            if not isinstance(part, dict):
                bits.append(str(part))
                continue
            kind = part.get("type")
            if kind == "text":
                bits.append(part.get("text") or "")
            elif kind == "image_url":
                bits.append(_image_placeholder(part))
            else:
                bits.append(str(part.get("text") or kind or ""))
        return "\n".join(b for b in bits if b)
    return str(content)


def _image_placeholder(part: dict) -> str:
    text = (part.get("text") or "").strip()
    if text:
        return text
    return "[image]"


def image_user_content(text: str, media: "list[MediaPart]") -> "str | list":
    """User message content: string, or text + image_url parts when media is set."""
    if not media:
        return text
    parts: "list[dict]" = [{"type": "text", "text": text}]
    for item in media:
        parts.append({
            "type": "text",
            "text": f"[lmloop] Image: {item.caption()}",
        })
        parts.append(item.content_part())
    return parts


def detect_kind(data: bytes, name: str = "", content_type: str = "") -> str:
    """Classify bytes by magic, then suffix / Content-Type."""
    suffix = Path(name).suffix.lower()
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    magic = _kind_from_magic(data)
    if magic == KIND_IMAGE or magic == KIND_AUDIO or magic == KIND_PDF:
        return magic
    if magic == "zip":
        return _kind_from_zip(data, suffix) or KIND_ZIP
    if suffix in IMAGE_SUFFIXES or ctype.startswith("image/"):
        return KIND_IMAGE
    if suffix in AUDIO_SUFFIXES or ctype.startswith("audio/"):
        return KIND_AUDIO
    if suffix in PDF_SUFFIXES or ctype == "application/pdf":
        return KIND_PDF
    if suffix == ".docx" or ctype.endswith("wordprocessingml.document"):
        return KIND_DOCX
    if suffix == ".xlsx" or ctype.endswith("spreadsheetml.sheet"):
        return KIND_XLSX
    if suffix == ".pptx" or ctype.endswith("presentationml.presentation"):
        return KIND_PPTX
    if suffix in OLE_SUFFIXES:
        return KIND_BINARY
    if _looks_binary(data):
        return KIND_BINARY
    return KIND_TEXT


def extract_path(path: Path) -> Extracted:
    """Read a local file and extract text / optional image media."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return Extracted(KIND_BINARY, f"ERROR: {e}")
    return extract_bytes(data, name=path.name, label=str(path))


def attachment_excerpt(
    path: Path, max_lines: int = EXCERPT_MAX_LINES,
) -> "str | None":
    """Extracted text to inline for an @path, or None for text/images/dirs."""
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    extracted = extract_path(path)
    if extracted.kind not in INLINE_KINDS:
        return None
    lines = extracted.text.splitlines()
    limit = max(1, int(max_lines))
    body = "\n".join(lines[:limit])
    if len(lines) > limit:
        body += (
            f"\n... [{len(lines)} lines total; "
            "use read_file with start_line= to continue]"
        )
    return body


def extract_bytes(
    data: bytes,
    name: str = "",
    content_type: str = "",
    label: str = "",
) -> Extracted:
    """Extract from an in-memory blob (files and fetch_url)."""
    label = label or name or "bytes"
    kind = detect_kind(data, name=name, content_type=content_type)
    if kind == KIND_TEXT:
        return Extracted(KIND_TEXT, data.decode("utf-8", errors="replace"))
    if kind == KIND_IMAGE:
        return _extract_image(data, name=name, label=label)
    if kind == KIND_PDF:
        return _extract_pdf(data, label=label)
    if kind == KIND_DOCX:
        return _extract_docx(data, label=label)
    if kind == KIND_XLSX:
        return _extract_xlsx(data, label=label)
    if kind == KIND_PPTX:
        return _extract_pptx(data, label=label)
    if kind == KIND_AUDIO:
        return _extract_audio(data, name=name, label=label)
    if kind == KIND_ZIP:
        return _extract_zip(data, label=label)
    suffix = Path(name).suffix.lower()
    if suffix in OLE_SUFFIXES:
        return Extracted(
            KIND_BINARY,
            f"ERROR: {suffix} is an old Office format; save as "
            f".docx/.xlsx/.pptx and retry",
        )
    return Extracted(
        KIND_BINARY,
        f"ERROR: {label} is a binary file ({kind}); cannot read as text",
    )


def media_from_paths(paths: "list[Path]") -> "list[MediaPart]":
    """Load image MediaParts from existing files (skip dirs / non-images)."""
    out: "list[MediaPart]" = []
    for path in paths:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        extracted = extract_path(path)
        if extracted.kind == KIND_IMAGE and extracted.media:
            out.append(extracted.media)
    return out


# ------------------------------------------------------------------ detection

def _kind_from_magic(data: bytes) -> "str | None":
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return KIND_IMAGE
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return KIND_IMAGE
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return KIND_IMAGE
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return KIND_IMAGE
    if data[:2] == b"BM":
        return KIND_IMAGE
    if data[:4] == b"%PDF":
        return KIND_PDF
    if data[:2] == b"PK":
        return "zip"
    if data[:3] == b"ID3":
        return KIND_AUDIO
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return KIND_AUDIO
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"mp41", b"mp42", b"isom", b"M4B "):
            return KIND_AUDIO
    if data[:4] == b"fLaC" or data[:4] == b"OggS":
        return KIND_AUDIO
    return None


def _kind_from_zip(data: bytes, suffix: str) -> "str | None":
    if suffix == ".docx":
        return KIND_DOCX
    if suffix == ".xlsx":
        return KIND_XLSX
    if suffix == ".pptx":
        return KIND_PPTX
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return None
    if any(n.startswith("word/") for n in names):
        return KIND_DOCX
    if any(n.startswith("xl/") for n in names):
        return KIND_XLSX
    if any(n.startswith("ppt/") for n in names):
        return KIND_PPTX
    return None


def _looks_binary(data: bytes) -> bool:
    sample = data[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    textish = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
    return textish / len(sample) < 0.75


# ------------------------------------------------------------------ images

def _extract_image(data: bytes, name: str, label: str) -> Extracted:
    if len(data) > MAX_IMAGE_BYTES:
        return Extracted(
            KIND_IMAGE,
            f"ERROR: image too large ({len(data)} bytes, max {MAX_IMAGE_BYTES})",
        )
    mime = _image_mime(data, name)
    width, height = image_dimensions(data)
    media = MediaPart(label=label, mime=mime, data=data, width=width, height=height)
    return Extracted(KIND_IMAGE, media.caption(), media=media)


def _image_mime(data: bytes, name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in MIME_BY_SUFFIX:
        return MIME_BY_SUFFIX[suffix]
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "application/octet-stream"


def image_dimensions(data: bytes) -> "tuple[int, int]":
    """Return (width, height) or (0, 0) when headers are not parsed."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if data[:2] == b"BM" and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return abs(int(width)), abs(int(height))
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return int(width), int(height)
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg_dimensions(data)
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    return 0, 0


def _jpeg_dimensions(data: bytes) -> "tuple[int, int]":
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            return 0, 0
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[i + 5: i + 9])
            return int(width), int(height)
        if marker in (0xD8, 0xD9) or marker == 0xFF:
            i += 1
            continue
        if i + 4 > n:
            break
        seglen = struct.unpack(">H", data[i + 2: i + 4])[0]
        if seglen < 2:
            break
        i += 2 + seglen
    return 0, 0


def _webp_dimensions(data: bytes) -> "tuple[int, int]":
    # VP8X: bytes 24-29 are 24-bit width-1 / height-1 little endian
    if data[12:16] == b"VP8X" and len(data) >= 30:
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return w, h
    if data[12:16] == b"VP8 " and len(data) >= 30:
        # Lossy: 14 bytes into the VP8 bitstream after chunk header
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return int(width), int(height)
    return 0, 0


# ------------------------------------------------------------------ pdf / office / audio

def _clip(text: str) -> str:
    if len(text) <= MAX_EXTRACT_CHARS:
        return text
    return text[:MAX_EXTRACT_CHARS] + f"\n... [truncated, {len(text)} chars total]"


def _extract_pdf(data: bytes, label: str) -> Extracted:
    text, err = _pdftotext(data)
    if text is None:
        text, err = _pypdf_text(data)
    if text is None:
        hint = err or "install poppler (pdftotext) or pypdf"
        return Extracted(KIND_PDF, f"ERROR: cannot extract PDF text from {label}: {hint}")
    header = f"[pdf {label}]\n"
    return Extracted(KIND_PDF, header + _clip(text))


def _pdftotext(data: bytes) -> "tuple[str | None, str | None]":
    if not shutil.which("pdftotext"):
        return None, "pdftotext not on PATH (brew install poppler)"
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=data,
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return None, err or f"pdftotext exit {proc.returncode}"
    return proc.stdout.decode("utf-8", errors="replace"), None


def _pypdf_text(data: bytes) -> "tuple[str | None, str | None]":
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "pypdf not installed"
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    return "\n\n".join(pages), None


def _extract_docx(data: bytes, label: str) -> Extracted:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as e:
        return Extracted(KIND_DOCX, f"ERROR: invalid docx ({e})")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return Extracted(KIND_DOCX, f"ERROR: invalid docx XML: {e}")
    paras = []
    for p in root.iter(f"{W_NS}p"):
        texts = [t.text or "" for t in p.iter(f"{W_NS}t")]
        paras.append("".join(texts))
    body = "\n".join(paras).strip() or "(empty document)"
    return Extracted(KIND_DOCX, f"[docx {label}]\n" + _clip(body))


def _extract_xlsx(data: bytes, label: str) -> Extracted:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return Extracted(KIND_XLSX, f"ERROR: invalid xlsx ({e})")
    strings = _xlsx_shared_strings(zf)
    sheets = [
        n for n in zf.namelist()
        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
    ]
    blocks = []
    for name in sorted(sheets):
        try:
            xml = zf.read(name)
        except KeyError:
            continue
        rows = _xlsx_sheet_rows(xml, strings)
        if rows:
            blocks.append(f"# {name}\n" + "\n".join(rows))
    zf.close()
    body = "\n\n".join(blocks).strip() or "(empty workbook)"
    return Extracted(KIND_XLSX, f"[xlsx {label}]\n" + _clip(body))


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list:
    try:
        xml = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for si in root.iter(f"{S_NS}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{S_NS}t")))
    return out


def _xlsx_sheet_rows(xml: bytes, strings: list) -> list:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    rows_out = []
    for row in root.iter(f"{S_NS}row"):
        cells = []
        for c in row.iter(f"{S_NS}c"):
            v = c.find(f"{S_NS}v")
            raw = (v.text or "") if v is not None else ""
            if c.get("t") == "s":
                try:
                    raw = strings[int(raw)]
                except (ValueError, IndexError):
                    pass
            cells.append(raw.replace("\t", " ").replace("\n", " "))
        if any(cells):
            rows_out.append("\t".join(cells))
    return rows_out


def _extract_pptx(data: bytes, label: str) -> Extracted:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return Extracted(KIND_PPTX, f"ERROR: invalid pptx ({e})")
    slides = [
        n for n in zf.namelist()
        if n.startswith("ppt/slides/slide") and n.endswith(".xml")
    ]
    blocks = []
    for i, name in enumerate(sorted(slides), 1):
        try:
            xml = zf.read(name)
        except KeyError:
            continue
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        texts = [t.text or "" for t in root.iter(f"{A_NS}t") if t.text]
        if texts:
            blocks.append(f"# Slide {i}\n" + "\n".join(texts))
    zf.close()
    body = "\n\n".join(blocks).strip() or "(empty presentation)"
    return Extracted(KIND_PPTX, f"[pptx {label}]\n" + _clip(body))


def _extract_zip(data: bytes, label: str) -> Extracted:
    """List archive members in memory. Never copies files to the workspace."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return Extracted(KIND_ZIP, f"ERROR: invalid zip ({e})")
    rows = []
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                rows.append(f"  {info.filename}")
            else:
                rows.append(f"  {info.filename}  ({info.file_size} bytes)")
    body = "\n".join(rows) or "  (empty archive)"
    header = f"[zip {label}: {len(rows)} entries — listing only; do not copy into the workspace]"
    return Extracted(KIND_ZIP, header + "\n" + _clip(body))


def _extract_audio(data: bytes, name: str, label: str) -> Extracted:
    suffix = Path(name).suffix.lower() or ".wav"
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / ("audio" + suffix)
        src.write_bytes(data)
        text, err = _whisper(src)
    if text is None:
        hint = err or "install whisper-cli (whisper.cpp) or whisper"
        return Extracted(KIND_AUDIO, f"ERROR: cannot transcribe {label}: {hint}")
    return Extracted(KIND_AUDIO, f"[audio {label}]\n" + _clip(text.strip()))


def _whisper(path: Path) -> "tuple[str | None, str | None]":
    if shutil.which("whisper-cli"):
        return _whisper_cli(path)
    if shutil.which("whisper"):
        return _whisper_openai(path)
    return None, "whisper-cli or whisper not on PATH"


def _whisper_cli(path: Path) -> "tuple[str | None, str | None]":
    out_base = path.with_suffix("")
    try:
        proc = subprocess.run(
            ["whisper-cli", "-np", "-nt", "-f", str(path), "-otxt", "-of", str(out_base)],
            capture_output=True,
            timeout=WHISPER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    txt = Path(str(out_base) + ".txt")
    if txt.is_file():
        return txt.read_text(errors="replace"), None
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
        return None, err or f"whisper-cli exit {proc.returncode}"
    stdout = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    return (stdout or None), None if stdout else "whisper-cli produced no transcript"


def _whisper_openai(path: Path) -> "tuple[str | None, str | None]":
    dest = path.parent
    try:
        proc = subprocess.run(
            [
                "whisper", str(path), "--model", "tiny",
                "--output_format", "txt", "--output_dir", str(dest),
            ],
            capture_output=True,
            timeout=WHISPER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    txt = dest / (path.stem + ".txt")
    if txt.is_file():
        return txt.read_text(errors="replace"), None
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
        return None, err or f"whisper exit {proc.returncode}"
    return None, "whisper produced no transcript"
