"""tests/test_library_sorting.py

Regression tests for library sort parsing and ordering.
"""

import sqlite3


def _idx(text: str, needle: str) -> int:
    pos = text.find(needle)
    assert pos != -1, f"Expected to find '{needle}' in response"
    return pos


def test_library_accepts_combined_sort_token_and_sorts_desc(client, db_conn: sqlite3.Connection):
    """`sort=average_rating-desc` is normalized and applied as descending."""
    db_conn.execute("DELETE FROM games")
    db_conn.executemany(
        "INSERT INTO games (name, store, store_id, average_rating) VALUES (?, ?, ?, ?)",
        [
            ("Rating Low", "steam", "100", 10.0),
            ("Rating Mid", "steam", "101", 55.0),
            ("Rating High", "steam", "102", 90.0),
        ],
    )
    db_conn.commit()

    resp = client.get("/library?sort=average_rating-desc")
    assert resp.status_code == 200

    text = resp.text
    assert _idx(text, "Rating High") < _idx(text, "Rating Mid") < _idx(text, "Rating Low")

    # Template state reflects normalized values so subsequent actions keep URL params coherent.
    assert 'name="sort" value="average_rating"' in text
    assert 'name="order" value="desc"' in text
