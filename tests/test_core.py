import pytest

from app.core import greet


def test_greet_default():
    assert greet() == "Hello, world!"


def test_greet_name():
    assert greet("Wilson") == "Hello, Wilson!"


def test_greet_strips_whitespace():
    assert greet("  Wilson  ") == "Hello, Wilson!"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_greet_rejects_blank(value):
    with pytest.raises(ValueError, match="must not be empty"):
        greet(value)
