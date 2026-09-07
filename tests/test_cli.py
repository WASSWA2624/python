from app.cli import main


def test_main_default(capsys):
    assert main([]) == 0
    assert capsys.readouterr().out == "Hello, world!\n"


def test_main_with_name(capsys):
    assert main(["Wilson"]) == 0
    assert capsys.readouterr().out == "Hello, Wilson!\n"


def test_main_blank_name(capsys):
    assert main(["   "]) == 1
    assert "must not be empty" in capsys.readouterr().err
