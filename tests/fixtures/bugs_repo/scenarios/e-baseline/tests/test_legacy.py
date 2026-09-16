"""Already failing before the agent arrived, and nothing to do with the task.

Every real repository has one of these. The agent is responsible for the tests it touched
and the ones it was asked about — not for this.
"""

from chaos.pages import paginate


def test_pages_are_tuples_as_they_were_in_version_0():
    # the library returned tuples once; it returns lists now and this was never updated
    assert paginate([1, 2], 1) == [(1,), (2,)]
