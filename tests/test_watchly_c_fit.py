from app.watchly_c.fit import fits_seed, fits_theme, theme_axis_ids
from app.watchly_c.relevance import Seed

ACTION, SCIFI, CRIME, THRILLER, DRAMA, MYSTERY, COMEDY = 28, 878, 80, 53, 18, 9648, 35
KAIJU, ALIEN, SUPERHERO, COMIC = 1, 2, 9715, 9717


def title(genres, keywords=()):
    return {"genre_ids": list(genres), "keywords": {"keywords": [{"id": k} for k in keywords]}}


def test_kill_bill_is_not_in_a_pacific_rim_row():
    seed = Seed(genres=(ACTION, SCIFI), language="en", year=2013, keywords=(KAIJU,), every_keyword=frozenset({KAIJU}))
    kill_bill = title([ACTION, CRIME, THRILLER], [77])
    assert not fits_seed(kill_bill, set(seed.genres), seed.every_keyword)


def test_a_shared_keyword_is_enough():
    assert fits_seed(title([SCIFI], [KAIJU]), {ACTION, SCIFI}, {KAIJU})


def test_two_shared_genres_are_enough_without_a_keyword():
    assert fits_seed(title([ACTION, SCIFI, THRILLER]), {ACTION, SCIFI}, {KAIJU})


def test_rashomon_is_not_in_an_alien_row():
    assert not fits_seed(title([CRIME, DRAMA, MYSTERY], [5]), {SCIFI, THRILLER}, {ALIEN})


def test_a_one_genre_seed_needs_that_genre():
    assert fits_seed(title([COMEDY, DRAMA]), {COMEDY}, set())
    assert not fits_seed(title([DRAMA]), {COMEDY}, set())


def test_the_sopranos_is_not_in_a_superhero_row():
    sopranos = title([CRIME, DRAMA], [3])
    assert not fits_theme(sopranos, {SUPERHERO}, {ACTION})
    assert fits_theme(title([ACTION], [SUPERHERO]), {SUPERHERO}, {ACTION})


def test_the_office_is_not_in_a_comic_row():
    assert not fits_theme(title([COMEDY], [4]), {COMIC}, {COMEDY})


def test_a_row_without_a_keyword_needs_one_of_its_genres():
    assert fits_theme(title([COMEDY]), set(), {COMEDY, DRAMA})
    assert not fits_theme(title([ACTION]), set(), {COMEDY, DRAMA})


def test_axis_ids_split_the_row_values():
    assert theme_axis_ids({"keyword": "9715-9717", "genre": "28", "country": "US"}) == ({9715, 9717}, {28})
    assert theme_axis_ids({"era": "1990"}) == (set(), set())
