from chaos.fetch import title_length


def test_example_com_is_reachable():
    assert title_length("https://example.com") > 100
