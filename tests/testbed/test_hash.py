"""The candidate hash matches the herd's byte for byte (issue #10 test vector)."""

from testbed.problem_api import candidate_hash


def test_issue_10_test_vector(tmp_path):
    files = {
        "a.py": b"print(1)\n",
        "sub/b.txt": b"hello",
        "manifest.yaml": b"kind: patch\n",
        "__pycache__/a.cpython-311.pyc": b"\x00compiled",
        "sub/c.pyc": b"\x00compiled",
    }
    for relative_path, content in files.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    assert (
        candidate_hash(tmp_path, "baseline@0")
        == "d4f39c30386a54508697b590e540898f913850422c6403723f66f232b98b1cb9"
    )
    assert (
        candidate_hash(tmp_path, "baseline@1")
        == "b11e4ba0c6a40c185e872cce7420919d06b7b7736dcc2a7c2cdeb74f2b20b0b6"
    )


def test_sort_is_plain_string_order_and_dotfiles_count(tmp_path):
    for relative_path in ("a.b", "a/b", "a-b"):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative_path)
    hash_before_dotfile = candidate_hash(tmp_path)
    (tmp_path / ".hidden").write_text("x")
    assert candidate_hash(tmp_path) != hash_before_dotfile
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "manifest.yaml").write_text(
        "kept"
    )  # only the top-level manifest is excluded
    hash_with_nested_manifest = candidate_hash(tmp_path)
    (tmp_path / "manifest.yaml").write_text("ignored")
    assert candidate_hash(tmp_path) == hash_with_nested_manifest
