"""The candidate hash is a contract with the testbed (Herd Testbed section 11.2, issue #10)."""

from pathlib import Path

from vlah.core.types.candidates import candidate_hash, payload_text

# Published on issue #10. The testbed's hash must produce the same values.
HASH_AT_BASELINE_0 = "d4f39c30386a54508697b590e540898f913850422c6403723f66f232b98b1cb9"
HASH_AT_BASELINE_1 = "b11e4ba0c6a40c185e872cce7420919d06b7b7736dcc2a7c2cdeb74f2b20b0b6"


def write_example_payload(directory: Path) -> None:
    (directory / "sub").mkdir()
    (directory / "__pycache__").mkdir()
    (directory / "a.py").write_bytes(b"print(1)\n")
    (directory / "sub" / "b.txt").write_bytes(b"hello")
    (directory / "manifest.yaml").write_bytes(b"kind: patch\n")
    (directory / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\x00compiled")
    (directory / "sub" / "c.pyc").write_bytes(b"\x00compiled")


def test_hash_matches_the_vector_published_to_the_testbed(tmp_path: Path) -> None:
    """Excluded files must not count, and the same payload on another baseline differs."""
    write_example_payload(tmp_path)
    assert candidate_hash(tmp_path, "baseline@0") == HASH_AT_BASELINE_0
    assert candidate_hash(tmp_path, "baseline@1") == HASH_AT_BASELINE_1


def test_similarity_text_has_payload_only(tmp_path: Path) -> None:
    write_example_payload(tmp_path)
    text = payload_text(tmp_path)
    assert "print(1)" in text
    assert "hello" in text
    assert "kind: patch" not in text
    assert "compiled" not in text
