# auth_service fixture

A small service used by `tests/integration/test_semantic_search.py` to test that semantic
search finds code by *meaning*.

The point of it is one file: `app/auth/bucket.py` implements rate limiting and **never uses
the words "rate" or "limit"**. A lexical search for "where is rate limiting handled" finds
nothing here; an embedding model finds `TokenBucket`, because that is what a token bucket
is for.

Nothing else in the tree should mention those words either, or the test stops measuring
what it claims to.
