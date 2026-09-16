from datetime import UTC, datetime

from chaos.stamp import EPOCH, seconds_since_epoch


def test_the_epoch_is_the_epoch():
    assert EPOCH.year == 1970


def test_one_day_after_the_epoch():
    assert seconds_since_epoch(datetime(1970, 1, 2, tzinfo=UTC)) == 86_400
