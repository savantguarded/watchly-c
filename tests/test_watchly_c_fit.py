from app.watchly_c.fit import fits_seed
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
