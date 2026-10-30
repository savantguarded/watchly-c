import importlib.util
import random
from pathlib import Path

spec = importlib.util.spec_from_file_location("holdout", Path(__file__).parent.parent / "scripts" / "holdout.py")
holdout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(holdout)


def test_the_split_hides_only_liked_titles_and_keeps_them_out_of_the_visible_set():
    titles = [{"imdb_id": f"tt{n}", "type": "movie", "rating": 9.0 if n < 10 else 5.0} for n in range(20)]

    hidden, visible = holdout.split(titles, 0.2, random.Random(1))

    assert len(hidden) == 2
    assert all(t["rating"] >= 8 for t in hidden + visible)
    assert not {t["imdb_id"] for t in hidden} & {t["imdb_id"] for t in visible}


def test_the_score_counts_hidden_titles_found_in_any_row_and_their_best_rank():
    rows = [["a", "x", "h1"], ["h1", "y"], ["z"]]

    assert holdout.score(rows, {"h1", "h2"}) == {"hit_rate": 0.5, "mean_best_rank": 1.0}
    assert holdout.score(rows, {"h2"}) == {"hit_rate": 0.0, "mean_best_rank": 0.0}
