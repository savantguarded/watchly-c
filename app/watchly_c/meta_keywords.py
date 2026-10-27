# TMDB keywords that tag a film's credits or sequel status rather than what it is about.
# "duringcreditsstinger" sits on most superhero films, so it tops a heavy watcher's keyword
# scores and ends up as a catalog of its own.
META_KEYWORDS = frozenset(
    {
        "duringcreditsstinger",
        "aftercreditsstinger",
        "post-credits scene",
        "mid-credits scene",
        "end credits scene",
        "sequel",
    }
)


def is_meta_keyword(name: str | None) -> bool:
    return bool(name) and name.strip().lower() in META_KEYWORDS
