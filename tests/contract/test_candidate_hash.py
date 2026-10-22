"""The herd's candidate hash and the testbed's must agree byte for byte (TB section 11.2, #10).

Skipped until the testbed package exists. The testbed agents confirm the import
path of their hash on #10; update TESTBED_HASH_MODULE if it differs.
"""

from pathlib import Path

import pytest

from vlah.core.types.candidates import candidate_hash

pytestmark = pytest.mark.contract

TESTBED_HASH_MODULE = "testbed.problem_api"

# Payload layouts that differ in the ways a second implementation is likely to get wrong.
PAYLOADS: dict[str, dict[str, bytes]] = {
    "single_file": {"policy.py": b"x = 1\n"},
    "empty_file": {"policy.py": b""},
    "nested": {"a/b/c.txt": b"deep", "a/d.txt": b"shallow"},
    "sort_order": {"a.b": b"1", "a/b": b"2", "a-b": b"3"},
    "binary": {"blob.bin": bytes(range(256))},
    "unicode_path": {"données/é.txt": "π".encode()},
    "top_manifest_excluded": {"manifest.yaml": b"kind: patch\n", "p.py": b"1"},
    "nested_manifest_kept": {"sub/manifest.yaml": b"kind: patch\n", "p.py": b"1"},
    "pycache_anywhere": {"x/__pycache__/m.cpython-311.pyc": b"\0", "x/m.py": b"m"},
    "pyc_anywhere": {"lib/m.pyc": b"\0", "lib/m.py": b"m"},
}


def write_payload(directory: Path, files: dict[str, bytes]) -> None:
    for relative_path, content in files.items():
        file_path = directory / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)


@pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
def test_herd_and_testbed_hashes_agree(tmp_path: Path, payload_name: str) -> None:
    testbed_hashing = pytest.importorskip(
        TESTBED_HASH_MODULE, reason="the testbed package is not in this repo yet (#10)"
    )
    write_payload(tmp_path, PAYLOADS[payload_name])
    for target in ("baseline@0", "baseline@7"):
        assert testbed_hashing.candidate_hash(tmp_path, target) == candidate_hash(
            tmp_path, target
        )
