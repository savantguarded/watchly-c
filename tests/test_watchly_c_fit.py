from app.watchly_c.relevance import Seed, content_match

ACTION, SCIFI, CRIME, THRILLER, DRAMA, MYSTERY, COMEDY = 28, 878, 80, 53, 18, 9648, 35
KAIJU, ALIEN, MURDER = 1, 2, 3


def title(genres, keywords=(), director=None):
    return {
        "genres": [{"id": g} for g in genres],
        "keywords": {"keywords": [{"id": k} for k in keywords]},
        "credits": {"crew": [{"id": director, "job": "Director"}] if director else [], "cast": []},
    }


PACIFIC_RIM = Seed(
    genres=(ACTION, SCIFI),
    language="en",
    year=2013,
    keywords=(KAIJU, MURDER),
    every_keyword=frozenset({KAIJU, MURDER}),
    telling=(KAIJU,),
    crew=frozenset({7}),
)


def test_kill_bill_is_not_in_a_pacific_rim_row():
    assert not content_match(title([ACTION, CRIME, THRILLER], [77]), PACIFIC_RIM)[1]


def test_a_telling_keyword_ties_a_title():
    assert content_match(title([SCIFI], [KAIJU]), PACIFIC_RIM)[1]


def test_two_shared_genres_alone_no_longer_tie_a_title():
    assert not content_match(title([ACTION, SCIFI, THRILLER]), PACIFIC_RIM)[1]


def test_a_stock_tag_with_the_genres_needs_tmdb_to_tie_it_too():
    shared_tag = title([ACTION, SCIFI], [MURDER])

    assert not content_match(shared_tag, PACIFIC_RIM)[1]
    assert content_match(shared_tag, PACIFIC_RIM, signal=0.8)[1]


def test_the_same_director_ties_a_title():
    assert content_match(title([DRAMA], director=7), PACIFIC_RIM)[1]


def test_rashomon_is_not_in_an_alien_row():
    alien = Seed(genres=(SCIFI, THRILLER), language="en", year=1979, keywords=(ALIEN,), telling=(ALIEN,))
    assert not content_match(title([CRIME, DRAMA, MYSTERY], [5]), alien)[1]


def test_more_in_common_scores_higher():
    loose = content_match(title([SCIFI], [KAIJU]), PACIFIC_RIM)[0]
    close = content_match(title([ACTION, SCIFI], [KAIJU, MURDER], director=7), PACIFIC_RIM)[0]
    assert close > loose


def test_a_seed_with_no_telling_keyword_leans_on_tmdb_and_the_genres():
    sparse = Seed(genres=(DRAMA, CRIME), language="en", year=2008, keywords=())

    assert content_match(title([DRAMA, CRIME]), sparse, signal=0.8)[1]
    assert not content_match(title([DRAMA, CRIME]), sparse, signal=0.3)[1]
