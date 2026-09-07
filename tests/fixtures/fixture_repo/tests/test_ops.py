from fixture.ops import add, slugify, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(0, 4) == -4


def test_slugify():
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Mixed   Case  Text ") == "mixed-case-text"
