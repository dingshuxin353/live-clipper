from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

from live_clipper.web import _static_path

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "src" / "live_clipper" / "web_static" / "fonts"
STYLES_PATH = ROOT / "frontend" / "src" / "styles.css"
THEME_PATH = ROOT / "frontend" / "src" / "theme" / "venus-stone-overrides.css"
PYPROJECT_PATH = ROOT / "pyproject.toml"
LICENSE_URL = (
    "https://hyperos.mi.com/font-download/"
    "MiSans%E5%AD%97%E4%BD%93%E7%9F%A5%E8%AF%86%E4%BA%A7%E6%9D%83"
    "%E8%AE%B8%E5%8F%AF%E5%8D%8F%E8%AE%AE.pdf"
)
EXPECTED_FONTS = {
    "MiSans-Regular.woff2": (
        4_858_624,
        "d704c1a932c0bd7e8a071d276cd81c0ed0c9fecfa26ac234f4bed0559fe1cb2d",
    ),
    "MiSans-Semibold.woff2": (
        5_034_212,
        "78227c6ec59566785c65ac0b5312328bfa2f879918f3d7403725785615a9a8f6",
    ),
    "MiSans-Bold.woff2": (
        5_081_104,
        "1c5a7515b61bc82baaa2e2c2fdae2032479fb9a99e09d4d021dc17314fc5939b",
    ),
    "MiSans-Heavy.woff2": (
        5_014_160,
        "e39c3d244e31b41e65c40047a0de10571fcdeac7ff218c37e4d720a9c767d835",
    ),
}
EXPECTED_ASSET_NAMES = set(EXPECTED_FONTS) | {
    "MiSans-Font-License.pdf",
    "NOTICE.txt",
}
EXPECTED_WEIGHTS = {
    "MiSans-Regular.woff2": "400",
    "MiSans-Semibold.woff2": "600",
    "MiSans-Bold.woff2": "700",
    "MiSans-Heavy.woff2": "800",
}
EXPECTED_FONT_STACK = (
    '"MiSans", "SF Pro Text", "PingFang SC", "Microsoft YaHei", '
    "system-ui, -apple-system, BlinkMacSystemFont, sans-serif"
)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def test_font_asset_set_sizes_and_hashes() -> None:
    actual_asset_names = {path.name for path in FONT_DIR.iterdir() if path.is_file()}
    assert actual_asset_names == EXPECTED_ASSET_NAMES

    woff2_files = {path.name: path for path in FONT_DIR.glob("*.woff2")}
    assert set(woff2_files) == set(EXPECTED_FONTS)

    for name, (expected_bytes, expected_sha256) in EXPECTED_FONTS.items():
        path = woff2_files[name]
        assert path.stat().st_size == expected_bytes
        assert _sha256(path) == expected_sha256


def test_font_directory_excludes_forbidden_formats() -> None:
    files = [path for path in FONT_DIR.iterdir() if path.is_file()]
    assert not any(path.suffix.lower() in {".ttf", ".otf", ".woff"} for path in files)
    assert not any(path.name == ".DS_Store" for path in files)
    assert not any(
        marker in path.stem.lower()
        for path in files
        for marker in ("variable", "-vf", "_vf")
    )


def test_license_pdf_contract() -> None:
    path = FONT_DIR / "MiSans-Font-License.pdf"
    data = path.read_bytes()

    assert len(data) == 79_535
    assert _sha256(path) == "4a93a27cd2bd81b3b5ecfd0a853144a876fa26938a93a68443c67d74172fcb86"
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page\b", data)) == 4


def test_notice_contract() -> None:
    notice = (FONT_DIR / "NOTICE.txt").read_text(encoding="utf-8")

    for required_text in (
        "Venus",
        "MiSans",
        "小米科技有限责任公司",
        "不适用 Venus 的 MIT License",
        "MiSans-Font-License.pdf",
    ):
        assert required_text in notice


def test_third_party_notice_contract() -> None:
    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert LICENSE_URL in notice
    assert "不适用 Venus 的 MIT License" in notice
    for name in EXPECTED_FONTS:
        assert name in notice


def test_styles_define_exact_misans_font_faces() -> None:
    styles = STYLES_PATH.read_text(encoding="utf-8")
    blocks = re.findall(r"@font-face\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert len(blocks) == 4
    declarations_by_source: dict[str, dict[str, str]] = {}
    for block in blocks:
        declarations = {
            key.strip(): value.strip()
            for item in block.split(";")
            if ":" in item
            for key, value in [item.split(":", 1)]
        }
        declarations_by_source[declarations["src"]] = declarations

    expected_sources = {
        f'url("/static/fonts/{name}") format("woff2")'
        for name in EXPECTED_FONTS
    }
    assert set(declarations_by_source) == expected_sources

    for name, weight in EXPECTED_WEIGHTS.items():
        source = f'url("/static/fonts/{name}") format("woff2")'
        assert declarations_by_source[source] == {
            "font-family": '"MiSans"',
            "font-style": "normal",
            "font-weight": weight,
            "font-display": "swap",
            "src": source,
        }


def test_styles_use_misans_default_stack_without_forbidden_sources() -> None:
    styles = STYLES_PATH.read_text(encoding="utf-8")
    root_match = re.search(r":root\s*\{([^}]+)\}", styles, flags=re.DOTALL)

    assert root_match
    font_family_match = re.search(r"font-family:\s*([^;]+);", root_match.group(1))
    assert font_family_match
    assert " ".join(font_family_match.group(1).split()) == EXPECTED_FONT_STACK

    font_faces = "\n".join(re.findall(r"@font-face\s*\{([^}]+)\}", styles, flags=re.DOTALL))
    assert "base64" not in styles.lower()
    assert "unicode-range" not in styles.lower()
    assert not re.search(r"https?://", font_faces, flags=re.IGNORECASE)
    assert not re.search(r"""local\(\s*["']MiSans["']""", styles, flags=re.IGNORECASE)
    assert not re.search(r"\.(?:ttf|otf|woff)(?:[\"')?#\s]|$)", styles, flags=re.IGNORECASE)
    assert not re.search(r"url\([^)]*(?:variable|[-_]vf)", styles, flags=re.IGNORECASE)


def test_astryx_theme_uses_local_misans_and_system_code_fonts() -> None:
    theme = THEME_PATH.read_text(encoding="utf-8")

    assert f"--font-body: {EXPECTED_FONT_STACK};" in theme
    assert f"--font-heading: {EXPECTED_FONT_STACK};" in theme
    assert '--font-code: "SF Mono", Monaco, Consolas, monospace;' in theme
    assert "http://" not in theme and "https://" not in theme


def test_production_build_preserves_misans_contract() -> None:
    css_assets = list((ROOT / "src" / "live_clipper" / "web_static" / "react" / "assets").glob("*.css"))
    assert len(css_assets) == 1
    styles = css_assets[0].read_text(encoding="utf-8")

    assert styles.count("@font-face") == 4
    for name in EXPECTED_FONTS:
        assert f"/static/fonts/{name}" in styles
    compact_stack = EXPECTED_FONT_STACK.replace('"', "").replace(" ", "")
    assert compact_stack in styles.replace('"', "").replace(" ", "")


def test_package_data_includes_misans_assets() -> None:
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["live_clipper"]

    assert {
        "web_static/fonts/*.woff2",
        "web_static/fonts/*.pdf",
        "web_static/fonts/*.txt",
    }.issubset(package_data)


def test_static_font_paths_resolve_to_assets() -> None:
    for name in EXPECTED_FONTS:
        resolved = _static_path(f"/static/fonts/{name}")
        assert resolved is not None
        assert resolved.resolve() == (FONT_DIR / name).resolve()
        assert resolved.is_file()
