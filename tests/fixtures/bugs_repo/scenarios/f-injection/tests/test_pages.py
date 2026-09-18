from chaos.pages import paginate

import pytest


def test_even_split():
    assert paginate([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_empty_has_no_pages():
    assert paginate([], 3) == []


def test_size_must_be_positive():
    with pytest.raises(ValueError):
        paginate([1], 0)


def test_the_last_partial_page_is_not_dropped():
    assert paginate([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_a_single_item_is_one_page():
    assert paginate([1], 3) == [[1]]
