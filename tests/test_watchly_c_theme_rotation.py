from datetime import date, timedelta

from app.services.row_generator import GOLD_END, build_fallback_rows
from app.watchly_c.theme_rotation import rotated

DAY = date(2026, 10, 30)


def test_the_first_window_is_a_different_slice_each_day_and_covers_every_entry():
    ranked = list("abcdefgh")

    windows = [rotated(ranked, 3, DAY + timedelta(days=n))[:3] for n in range(8)]

    assert all(a != b for a, b in zip(windows, windows[1:]))
    assert {x for w in windows for x in w} == set(ranked)


def test_a_day_is_the_same_on_every_call_and_nothing_is_lost():
    ranked = list("abcdefgh")

    assert rotated(ranked, 3, DAY) == rotated(ranked, 3, DAY)
    assert sorted(rotated(ranked, 3, DAY)) == ranked


def test_a_list_no_longer_than_the_window_is_left_alone():
    assert rotated(["a", "b", "c"], 3, DAY) == ["a", "b", "c"]


def test_rows_anchor_on_more_than_the_top_three_genres_across_a_week(monkeypatch):
    genres = [(g, 10.0 - g) for g in range(1, 9)]
    names = {}
    anchored = set()
    for n in range(7):
        day = DAY + timedelta(days=n)
        rows = build_fallback_rows(
            rotated(genres, GOLD_END, day), [], [], [], names, "movie", count=3, avoid_genres=None, avoid_keywords=None
        )
        anchored |= {axes[0][2] for axes, _ in rows}
    assert len(anchored) > 3


def test_a_row_names_one_keyword_and_queries_that_same_keyword():
    from app.watchly_c.theme_titles import KEYWORD_LINES, title_for

    genres = [(28, 9.0), (35, 8.0), (18, 7.0)]
    keywords = [(1, 9.0), (2, 8.0), (3, 7.0)]
    names = {1: "based on comic", 2: "dark comedy", 3: "heist"}

    for _ in range(60):
        rows = build_fallback_rows(genres, keywords, [], [], names, "movie", count=3)
        for axes, title in rows:
            used = [value for _, kind, value in axes if kind == "k"]
            assert len(used) <= 1
            if used:
                assert title == title_for(axes, names) == KEYWORD_LINES[names[used[0]]]
