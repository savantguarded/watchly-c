from collections import Counter
from datetime import date, timedelta

from app.services.row_generator import build_fallback_rows
from app.watchly_c.theme_rotation import day_slice

DAY = date(2026, 10, 30)
NAMES = {
    1: "murder",
    2: "revenge",
    3: "heist",
    4: "serial killer",
    5: "detective",
    6: "spy",
    7: "conspiracy",
    8: "prison",
    9: "time travel",
    10: "dystopia",
    11: "superhero",
    12: "alien",
    13: "vampire",
    14: "zombie",
    15: "robot",
    16: "survival",
}


def test_each_day_takes_the_next_slice_and_the_pool_is_covered():
    ranked = list("abcdefghij")

    slices = [day_slice(ranked, 2, DAY + timedelta(days=n)) for n in range(5)]

    assert all(len(s) == 2 for s in slices)
    assert {x for s in slices for x in s} == set(ranked)


def test_a_day_is_the_same_on_every_call():
    assert day_slice(list("abcdefgh"), 3, DAY) == day_slice(list("abcdefgh"), 3, DAY)


def test_a_pool_no_bigger_than_the_slice_is_left_alone():
    assert day_slice(["a", "b"], 3, DAY) == ["a", "b"]


def _rows_for(day: date, count: int):
    genres = [(g, 20.0 - i) for i, g in enumerate([80, 18, 53, 28, 9648, 35, 878, 12, 14, 27, 10749, 36])]
    keywords = [(k, 20.0 - k) for k in NAMES]
    return build_fallback_rows(
        day_slice(genres, count + 1 + count // 3, day),
        day_slice(keywords, -(-2 * count // 3), day),
        [("US", 1.0)],
        [],
        NAMES,
        "movie",
        count=count,
        whole_slice=True,
    )


def test_no_keyword_row_comes_back_more_than_once_a_week():
    seen = Counter()
    for n in range(28):
        rows = _rows_for(DAY + timedelta(days=n), 3)
        seen.update({v for axes, _ in rows for _, kind, v in axes if kind == "k"})

    assert len(seen) == len(NAMES)
    assert max(seen.values()) <= 4


def test_the_day_still_builds_every_row_from_its_slice():
    for n in range(10):
        assert len(_rows_for(DAY + timedelta(days=n), 3)) == 3
        assert len(_rows_for(DAY + timedelta(days=n), 2)) == 2


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
