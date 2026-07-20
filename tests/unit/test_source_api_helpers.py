"""Unit tests for source API transport helpers."""

from api.routers.sources import _display_filename, _document_display_name


def test_display_filename_preserves_unicode_basename() -> None:
    assert _display_filename(r"C:\fakepath\中文资料.txt") == "中文资料.txt"


def test_display_filename_repairs_utf8_decoded_as_latin1() -> None:
    mojibake = "中文摄入测试.txt".encode().decode("latin-1")

    assert _display_filename(mojibake) == "中文摄入测试.txt"


def test_display_filename_repairs_two_mojibake_layers() -> None:
    mojibake = "中文摄入测试.txt".encode().decode("latin-1")
    double_mojibake = mojibake.encode().decode("latin-1")

    assert _display_filename(double_mojibake) == "中文摄入测试.txt"


def test_display_filename_repairs_three_mojibake_layers() -> None:
    value = "中文摄入测试.txt"
    for _ in range(3):
        value = value.encode().decode("latin-1")

    assert _display_filename(value) == "中文摄入测试.txt"


def test_display_filename_repairs_five_mojibake_layers() -> None:
    value = "中文摄入测试.txt"
    for _ in range(5):
        value = value.encode().decode("latin-1")

    assert _display_filename(value) == "中文摄入测试.txt"


def test_display_filename_repairs_windows_1252_mojibake() -> None:
    mojibake = "中文摄入测试.txt".encode().decode("cp1252")

    assert _display_filename(mojibake) == "中文摄入测试.txt"


def test_display_filename_repairs_mixed_latin1_cp1252_mojibake() -> None:
    mojibake = "ä¸­æ–‡æ‘„å…¥æµ‹è¯•.txt"

    assert _display_filename(mojibake) == "中文摄入测试.txt"


def test_display_filename_keeps_valid_latin1_name() -> None:
    assert _display_filename("résumé.pdf") == "résumé.pdf"


def test_document_display_name_falls_back_from_legacy_placeholder() -> None:
    assert (
        _document_display_name("service-runbook-v2.txt", "未命名文档") == "service-runbook-v2.txt"
    )
