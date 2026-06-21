from __future__ import annotations

import pytest

from live_clipper.utils import ensure_dir, read_json, read_required_text, write_json


def test_write_json_creates_parent_and_preserves_unicode(tmp_path):
    path = tmp_path / "nested" / "data.json"

    write_json(path, {"text": "直播切片", "items": [1, 2]})

    assert path.exists()
    assert read_json(path) == {"text": "直播切片", "items": [1, 2]}


def test_ensure_dir_returns_created_path(tmp_path):
    path = tmp_path / "output" / "week_023"

    result = ensure_dir(path)

    assert result == path
    assert path.is_dir()


def test_read_required_text_reports_missing_file_with_description(tmp_path):
    path = tmp_path / "missing.md"

    with pytest.raises(FileNotFoundError, match="cheap scan prompt"):
        read_required_text(path, "cheap scan prompt")
