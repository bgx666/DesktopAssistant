"""文档解析（fileparse）+ OCR（ocr）测试：monkeypatch 解析库，不依赖真实库。"""

from pathlib import Path

import pytest

import planner.fileparse as fp_mod
import planner.ocr as ocr_mod


# ── fileparse ──────────────────────────────────────────────

def test_parse_pdf_text(monkeypatch, tmp_path):
    fake_pdf = tmp_path / "doc.pdf"
    fake_pdf.write_bytes(b"%PDF-fake")

    class _FakePage:
        def get_text(self):
            return "第一页内容\n"

    class _FakeDoc:
        def __init__(self, path):
            self.pages = [_FakePage(), _FakePage()]

        def __iter__(self):
            return iter(self.pages)

        def close(self):
            pass

    monkeypatch.setattr(fp_mod, "_HAS_FITZ", True)
    monkeypatch.setattr(fp_mod, "fitz", type("F", (), {"open": staticmethod(_FakeDoc)}))
    text = fp_mod.parse_pdf_text(fake_pdf)
    assert text == "第一页内容\n\n第一页内容"


def test_parse_docx_text(monkeypatch, tmp_path):
    fake_docx = tmp_path / "doc.docx"
    fake_docx.write_bytes(b"docx-fake")

    class _FakePara:
        def __init__(self, t):
            self.text = t

    class _FakeDocx:
        def __init__(self, path):
            self.paragraphs = [_FakePara("标题"), _FakePara("正文内容"), _FakePara("  ")]

    monkeypatch.setattr(fp_mod, "_HAS_DOCX", True)
    monkeypatch.setattr(fp_mod, "docx", type("D", (), {"Document": staticmethod(_FakeDocx)}))
    text = fp_mod.parse_docx_text(fake_docx)
    assert text == "标题\n正文内容"


def test_parse_file_unsupported(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"\x00\x01")
    assert fp_mod.parse_file(str(f)) is None
    assert fp_mod.parse_file(str(tmp_path / "missing.pdf")) is None


def test_save_attachment_text(tmp_path):
    out = fp_mod.save_attachment_text(tmp_path, r"D:\a\b.pdf", "解析内容")
    assert out.parent == tmp_path / "attachments"
    assert out.read_text(encoding="utf-8") == "解析内容"
    # 同一源路径 → 同一 hash，幂等覆盖
    out2 = fp_mod.save_attachment_text(tmp_path, r"D:\a\b.pdf", "新内容")
    assert out2 == out
    assert out2.read_text(encoding="utf-8") == "新内容"


# ── ocr ────────────────────────────────────────────────────

def test_ocr_disabled_without_dep(monkeypatch):
    monkeypatch.setattr(ocr_mod, "_HAS_DEP", False)
    c = ocr_mod.OcrClient()
    assert not c.enabled
    assert c.recognize(b"png-bytes") is None


def test_ocr_recognize(monkeypatch):
    calls = {}

    class _FakeOCR:
        def __call__(self, img):
            calls["img"] = img
            return [([0, 0, 10, 10], "你好 世界", 0.99)], None

    monkeypatch.setattr(ocr_mod, "_HAS_DEP", True)
    monkeypatch.setattr(ocr_mod, "RapidOCR", _FakeOCR)
    monkeypatch.setattr(ocr_mod, "_decode_image", lambda b: "IMG")

    c = ocr_mod.OcrClient()
    assert c.enabled
    text = c.recognize(b"png-bytes")
    assert text == "你好 世界"
    assert calls["img"] == "IMG"
    # 空结果 → None
    monkeypatch.setattr(ocr_mod, "_decode_image", lambda b: None)
    assert c.recognize(b"x") is None


def test_ocr_recognize_path(monkeypatch, tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"PNGDATA")

    class _FakeOCR:
        def __call__(self, img):
            return [([0, 0, 5, 5], "OK", 0.9)], None

    monkeypatch.setattr(ocr_mod, "_HAS_DEP", True)
    monkeypatch.setattr(ocr_mod, "RapidOCR", _FakeOCR)
    monkeypatch.setattr(ocr_mod, "_decode_image", lambda b: "IMG")
    c = ocr_mod.OcrClient()
    assert c.recognize_path(str(f)) == "OK"
    assert c.recognize_path(str(tmp_path / "missing.png")) is None
