import pytest

from solc_bench.config import load_benchmarks


def write_suite(root, top_level, included):
    (root / "benchmarks.toml").write_text(top_level, encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "benchmarks.toml").write_text(included, encoding="utf-8")


def test_include_may_reuse_a_top_level_name(tmp_path):
    write_suite(tmp_path, 'include = ["sub"]\n[foo]\n', "[foo]\n")
    assert list(load_benchmarks(tmp_path)) == ["foo", "sub/foo"]


def test_include_rejects_duplicate_names(tmp_path):
    write_suite(tmp_path, 'include = ["sub", "./sub"]\n', "[foo]\n")
    with pytest.raises(ValueError, match="duplicate benchmark name 'sub/foo'"):
        load_benchmarks(tmp_path)
