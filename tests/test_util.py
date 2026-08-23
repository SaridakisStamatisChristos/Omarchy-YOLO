from __future__ import annotations

import pytest

from omarchy_yolo.util import YoloError, extract_json_object, slug


def test_extract_json_object_from_mixed_output() -> None:
    text = 'noise {"old":1}\n```json\n{"verdict":"pass","findings":[]}\n```'
    assert extract_json_object(text)["verdict"] == "pass"


def test_extract_json_object_rejects_missing_json() -> None:
    with pytest.raises(YoloError):
        extract_json_object("nothing structured here")


def test_slug_is_branch_safe() -> None:
    assert slug(" Parser / Fix !! ") == "parser-fix"



def test_private_dir_rejects_symlink(tmp_path) -> None:
    from omarchy_yolo.util import ensure_private_dir

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(YoloError, match="symlink"):
        ensure_private_dir(link)


def test_terminal_safe_strips_control_sequences() -> None:
    from omarchy_yolo.util import terminal_safe

    assert terminal_safe("ok\x1b[31mRED\x1b[0m\nnext", single_line=True) == "okRED next"



def test_private_file_rejects_symlink(tmp_path) -> None:
    from omarchy_yolo.util import open_private_binary

    target = tmp_path / "target"
    target.write_text("do not overwrite")
    link = tmp_path / "log"
    link.symlink_to(target)
    with pytest.raises(YoloError, match="safely open"):
        open_private_binary(link)
    assert target.read_text() == "do not overwrite"
