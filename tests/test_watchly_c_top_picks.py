from app.core.config import settings
from app.core.settings import get_default_settings
from app.services.profile.scoring import ScoringService
from app.services.recommendation.candidate_sources import CandidateFetcher
from app.watchly_c.top_picks import FLOOR, rank


class FitScorer:
    def __init__(self, fits: dict[int, float]):
        self.fits = fits

    def score_item(self, item, profile):
        return self.fits[item["id"]]


def title(tmdb_id, avg, votes=2000, popularity=50):
    return {"id": tmdb_id, "vote_average": avg, "vote_count": votes, "popularity": popularity}


def test_titles_under_the_floor_are_dropped_however_well_they_fit():
    items = [title(1, 6.0), title(2, 8.0), title(3, 9.0, votes=40)]
    scorer = FitScorer({1: 1.0, 2: 0.5, 3: 1.0})
    assert [i["id"] for _, i in rank(items, None, scorer, "movie")] == [2]
    assert FLOOR > 6.5


def test_a_better_rated_title_beats_a_slightly_closer_one():
    items = [title(1, 6.9), title(2, 8.2)]
    scorer = FitScorer({1: 0.80, 2: 0.70})
    assert [i["id"] for _, i in rank(items, None, scorer, "movie")] == [2, 1]


def test_popularity_earns_nothing():
    items = [title(1, 7.6, popularity=900), title(2, 7.6, popularity=20)]
    scorer = FitScorer({1: 0.6, 2: 0.6})
    scores = [s for s, _ in rank(items, None, scorer, "movie")]
    assert scores[0] == scores[1]


class RecordingTMDB:
    def __init__(self):
        self.sorts = []

    def get_discover(self, mtype, **params):
        self.sorts.append(params["sort_by"])


def test_top_picks_discover_sorts_by_rating_when_on_and_popularity_when_off():
    user = get_default_settings()
    user.popularity = "balanced"
    try:
        for on, expected in ((True, "vote_average.desc"), (False, "popularity.desc")):
            settings.QUALITY_TOP_PICKS = on
            tmdb = RecordingTMDB()
            CandidateFetcher(tmdb, user, ScoringService())._add_discover_task([], "movie", None)
            assert tmdb.sorts == [expected]
    finally:
        settings.QUALITY_TOP_PICKS = True
