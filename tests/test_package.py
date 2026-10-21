import vlah
from vlah.cli import main


def test_version_is_exposed() -> None:
    assert vlah.__version__


def test_cli_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("vlah ")
