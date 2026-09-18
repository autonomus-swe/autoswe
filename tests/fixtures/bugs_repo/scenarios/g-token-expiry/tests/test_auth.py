from chaos.auth import current_user, decode_token, issue_token


def test_a_token_round_trips():
    assert decode_token(issue_token("alice"))["sub"] == "alice"


def test_an_issued_token_records_when_it_stops_being_valid():
    assert decode_token(issue_token("alice"))["exp"] > 0


def test_current_user_reads_the_subject():
    assert current_user(issue_token("alice")) == "alice"
