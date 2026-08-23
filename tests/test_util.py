from __future__ import annotations

import pytest

from omarchy_yolo.util import YoloError, extract_json_object, slug


def test_extract_json_object_from_mixed_output() -> None:
    text = 'noise {"old":1}\n```json\n{"verdict":"pass","findings":[]}\n```'
    assert extract_json_object(text)["verdict"] == "pass"


def test_extract_json_object_rejects_missing_json() -> None:
    with pytest.raises(YoloError): extract_json_object("nothing structured here")


def test_slug_is_branch_safe() -> None:
    assert slug(" Parser / Fix !! ") == "parser-fix"
