from __future__ import annotations

import hashlib
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG_SHA256 = "081c5477e8fc0fb6ae4b6fd69ed6ba19cb25ba9c72c60473a6255a65b61c1e74"
ASSETS = {
    "desktop/assets/trayTemplate.png": (
        (16, 16),
        "3f7b236fe18b45cf9e25d49f837fc84817de64ab6f3bf430e86222e7e86d558f",
    ),
    "desktop/assets/trayTemplate@2x.png": (
        (32, 32),
        "b4440f73098de941f97aedabb5acf51aae01d2b704c15d1a6d554e4ef4ebc25d",
    ),
}
OLD_SHA256S = {
    "bb166877b76636dad0589b32a6ecf39f1c1d370c88dadb14e910bf98cae5e173",
    "6bdd2444835236372cb1083c0b4264bf691890f8917c79feaa8529cf803d470b",
}


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_tray_png_assets_match_size_and_sha256_contract():
    actual_hashes = set()
    for relative_path, (expected_size, expected_sha256) in ASSETS.items():
        path = ROOT / relative_path
        assert path.is_file()
        data = path.read_bytes()
        actual_sha256 = hashlib.sha256(data).hexdigest()
        actual_hashes.add(actual_sha256)
        assert _png_size(data) == expected_size
        assert actual_sha256 == expected_sha256

    assert actual_hashes.isdisjoint(OLD_SHA256S)


def test_tray_runtime_keeps_template_image_contract():
    main_js = (ROOT / "desktop/main.js").read_text(encoding="utf-8")

    assert 'path.join(__dirname, "assets", "trayTemplate.png")' in main_js
    assert "icon.setTemplateImage(true)" in main_js


def test_tray_assets_remain_in_electron_package():
    builder_config = (ROOT / "desktop/electron-builder.yml").read_text(encoding="utf-8")

    assert "- assets/**" in builder_config


def test_tray_svg_exists_and_is_unchanged_from_lane_baseline():
    svg = ROOT / "desktop/assets/trayTemplate.svg"

    assert svg.is_file()
    assert hashlib.sha256(svg.read_bytes()).hexdigest() == SVG_SHA256
