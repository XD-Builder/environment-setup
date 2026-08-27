"""Tests for file extraction (images, OOXML, PDF/whisper CLIs)."""

import io
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from lmloop.extract import (
    KIND_AUDIO,
    KIND_BINARY,
    KIND_DOCX,
    KIND_IMAGE,
    KIND_PDF,
    KIND_PPTX,
    KIND_TEXT,
    KIND_XLSX,
    KIND_ZIP,
    MAX_IMAGE_BYTES,
    detect_kind,
    extract_bytes,
    extract_path,
    flatten_content,
    image_dimensions,
    image_user_content,
    media_from_paths,
)


# 1x1 transparent PNG (IHDR width/height at bytes 16-23).
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _jpeg(width=8, height=6) -> bytes:
    sof = (
        bytes([0xFF, 0xC0, 0x00, 0x0B, 0x08])
        + struct.pack(">HH", height, width)
        + bytes([1, 1, 0x11, 0])
    )
    return bytes([0xFF, 0xD8]) + sof + bytes([0xFF, 0xD9])


def _docx_bytes(paragraphs) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{body}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", document)
        zf.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


def _xlsx_bytes(rows) -> bytes:
    strings = []
    cells_xml = []
    for r_i, row in enumerate(rows, 1):
        bits = []
        for c_i, val in enumerate(row):
            idx = len(strings)
            strings.append(val)
            col = chr(ord("A") + c_i)
            bits.append(
                f'<c r="{col}{r_i}" t="s"><v>{idx}</v></c>'
            )
        cells_xml.append(f'<row r="{r_i}">' + "".join(bits) + "</row>")
    sst = "".join(f"<si><t>{s}</t></si>" for s in strings)
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared = f'<?xml version="1.0"?><sst xmlns="{ns}">{sst}</sst>'
    sheet = (
        f'<?xml version="1.0"?><worksheet xmlns="{ns}">'
        f'<sheetData>{"".join(cells_xml)}</sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def _pptx_bytes(texts) -> bytes:
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    runs = "".join(f'<a:t xmlns:a="{ns}">{t}</a:t>' for t in texts)
    slide = f'<?xml version="1.0"?><sld>{runs}</sld>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide)
    return buf.getvalue()


class DetectKindTests(unittest.TestCase):
    def test_png_magic(self):
        self.assertEqual(detect_kind(PNG_1X1, "x.bin"), KIND_IMAGE)

    def test_pdf_magic(self):
        self.assertEqual(detect_kind(b"%PDF-1.6\n1 0 obj", "x.txt"), KIND_PDF)

    def test_utf8_text(self):
        self.assertEqual(detect_kind(b"hello\nworld"), KIND_TEXT)
        self.assertEqual(detect_kind("中文标题\n".encode("utf-8")), KIND_TEXT)

    def test_nul_is_binary(self):
        self.assertEqual(detect_kind(b"abc\x00def"), KIND_BINARY)

    def test_docx_zip_namelist(self):
        data = _docx_bytes(["hi"])
        self.assertEqual(detect_kind(data), KIND_DOCX)


class ImageTests(unittest.TestCase):
    def test_png_dimensions_and_caption(self):
        extracted = extract_bytes(PNG_1X1, name="shot.png", label="shot.png")
        self.assertEqual(extracted.kind, KIND_IMAGE)
        self.assertIsNotNone(extracted.media)
        self.assertEqual(image_dimensions(PNG_1X1), (1, 1))
        self.assertIn("1x1", extracted.text)
        self.assertIn("image/png", extracted.text)
        self.assertNotIn("\x89PNG", extracted.text)

    def test_jpeg_dimensions(self):
        data = _jpeg(12, 9)
        self.assertEqual(image_dimensions(data), (12, 9))
        self.assertEqual(detect_kind(data, "a.jpg"), KIND_IMAGE)

    def test_image_too_large(self):
        data = PNG_1X1 + b"\x00" * (MAX_IMAGE_BYTES + 1)
        extracted = extract_bytes(data, name="big.png")
        self.assertIn("ERROR: image too large", extracted.text)
        self.assertIsNone(extracted.media)

    def test_media_from_paths_skips_text(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            png = root / "a.png"
            txt = root / "b.txt"
            png.write_bytes(PNG_1X1)
            txt.write_text("hi\n")
            media = media_from_paths([png, txt, root])
            self.assertEqual(len(media), 1)
            self.assertEqual(media[0].mime, "image/png")

    def test_image_user_content_multipart(self):
        extracted = extract_bytes(PNG_1X1, name="a.png", label="a.png")
        content = image_user_content("see this", [extracted.media])
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("see this", content[0]["text"])
        self.assertEqual(content[-1]["type"], "image_url")
        self.assertTrue(
            content[-1]["image_url"]["url"].startswith("data:image/png;base64,")
        )
        self.assertIsInstance(image_user_content("plain", []), str)


class OfficeTests(unittest.TestCase):
    def test_docx_paragraphs(self):
        data = _docx_bytes(["Hello", "World"])
        extracted = extract_bytes(data, name="note.docx", label="note.docx")
        self.assertEqual(extracted.kind, KIND_DOCX)
        self.assertIn("Hello", extracted.text)
        self.assertIn("World", extracted.text)

    def test_attachment_excerpt_inlines_docx(self):
        from lmloop.extract import attachment_excerpt
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "note.docx"
            path.write_bytes(_docx_bytes(["Hello", "World"]))
            excerpt = attachment_excerpt(path)
            self.assertIsNotNone(excerpt)
            self.assertIn("Hello", excerpt)
            self.assertIn("World", excerpt)
            txt = Path(d) / "a.py"
            txt.write_text("print(1)\n")
            self.assertIsNone(attachment_excerpt(txt))

    def test_xlsx_cells(self):
        data = _xlsx_bytes([["Name", "Qty"], ["apples", "3"]])
        extracted = extract_bytes(data, name="t.xlsx")
        self.assertEqual(extracted.kind, KIND_XLSX)
        self.assertIn("apples", extracted.text)
        self.assertIn("3", extracted.text)

    def test_pptx_slide_text(self):
        data = _pptx_bytes(["Title slide", "Bullet"])
        extracted = extract_bytes(data, name="d.pptx")
        self.assertEqual(extracted.kind, KIND_PPTX)
        self.assertIn("Title slide", extracted.text)
        self.assertIn("Slide 1", extracted.text)

    def test_old_doc_errors(self):
        extracted = extract_bytes(b"\xd0\xcf\x11\xe0", name="old.doc")
        self.assertEqual(extracted.kind, KIND_BINARY)
        self.assertIn("ERROR", extracted.text)
        self.assertIn(".doc", extracted.text)


class ZipTests(unittest.TestCase):
    def test_zip_lists_members_in_place(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "hello")
            zf.writestr("nested/a.py", "x = 1\n")
        data = buf.getvalue()
        self.assertEqual(detect_kind(data, "z.zip"), KIND_ZIP)
        extracted = extract_bytes(data, name="z.zip", label="/tmp/z.zip")
        self.assertEqual(extracted.kind, KIND_ZIP)
        self.assertIn("readme.txt", extracted.text)
        self.assertIn("nested/a.py", extracted.text)
        self.assertIn("do not copy", extracted.text)
        self.assertNotIn("hello", extracted.text)


class PdfWhisperTests(unittest.TestCase):
    def test_pdftotext_cli(self):
        pdf = b"%PDF-1.4\n%fake"
        proc = mock.Mock(returncode=0, stdout=b"Hello PDF\n", stderr=b"")
        with mock.patch("lmloop.extract.shutil.which", return_value="/usr/bin/pdftotext"), \
             mock.patch("lmloop.extract.subprocess.run", return_value=proc) as run:
            extracted = extract_bytes(pdf, name="a.pdf", label="a.pdf")
        self.assertEqual(extracted.kind, KIND_PDF)
        self.assertIn("Hello PDF", extracted.text)
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0][0], "pdftotext")

    def test_pdf_missing_tools(self):
        pdf = b"%PDF-1.4\n%fake"
        with mock.patch("lmloop.extract.shutil.which", return_value=None), \
             mock.patch("lmloop.extract._pypdf_text", return_value=(None, "pypdf not installed")):
            extracted = extract_bytes(pdf, name="a.pdf")
        self.assertIn("ERROR: cannot extract PDF", extracted.text)

    def test_whisper_cli(self):
        audio = b"ID3\x04fake-mp3"
        proc = mock.Mock(returncode=0, stdout=b"", stderr=b"")

        def fake_run(argv, **kwargs):
            out_base = argv[argv.index("-of") + 1]
            Path(out_base + ".txt").write_text("hello audio\n")
            return proc

        with mock.patch("lmloop.extract.shutil.which", side_effect=lambda n: n == "whisper-cli"), \
             mock.patch("lmloop.extract.subprocess.run", side_effect=fake_run):
            extracted = extract_bytes(audio, name="a.mp3", label="a.mp3")
        self.assertEqual(extracted.kind, KIND_AUDIO)
        self.assertIn("hello audio", extracted.text)

    def test_audio_missing_whisper(self):
        with mock.patch("lmloop.extract.shutil.which", return_value=None):
            extracted = extract_bytes(b"ID3xx", name="a.mp3")
        self.assertIn("ERROR: cannot transcribe", extracted.text)


class FlattenContentTests(unittest.TestCase):
    def test_drops_data_uri(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        flat = flatten_content(content)
        self.assertIn("hello", flat)
        self.assertIn("[image]", flat)
        self.assertNotIn("base64", flat)
        self.assertNotIn("AAAA", flat)

    def test_string_passthrough(self):
        self.assertEqual(flatten_content("abc"), "abc")
        self.assertEqual(flatten_content(None), "")


class ExtractPathTests(unittest.TestCase):
    def test_text_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.py"
            p.write_text("x = 1\n")
            extracted = extract_path(p)
            self.assertEqual(extracted.kind, KIND_TEXT)
            self.assertIn("x = 1", extracted.text)

    def test_png_file_not_mojibake(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.png"
            p.write_bytes(PNG_1X1)
            extracted = extract_path(p)
            self.assertEqual(extracted.kind, KIND_IMAGE)
            self.assertNotIn("\x89", extracted.text)
            self.assertIn("1x1", extracted.text)


if __name__ == "__main__":
    unittest.main()
