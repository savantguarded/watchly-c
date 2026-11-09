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


# Keywords on so many titles that sharing one says little about two titles being alike. They
# still count toward a match, but on their own they don't put a title in a seed's row.
BROAD_KEYWORDS = frozenset(
    {
        "based on novel or book",
        "based on comic",
        "based on true story",
        "based on short story",
        "based on play or musical",
        "biography",
        "woman director",
        "independent film",
        "remake",
        "reboot",
        "prequel",
        "spin off",
        "miniseries",
        "anthology",
        "3d",
        "murder",
        "death",
        "dying and death",
        "violence",
        "love",
        "romance",
        "friendship",
        "family",
        "family relationships",
        "relationship",
        "husband wife relationship",
        "father son relationship",
        "father daughter relationship",
        "mother son relationship",
        "mother daughter relationship",
        "brother brother relationship",
        "brother sister relationship",
        "sister sister relationship",
        "marriage",
        "divorce",
        "teenager",
        "loss of loved one",
        "grief",
        "new york city",
        "los angeles, california",
        "london, england",
        "paris, france",
        "usa",
        "sex",
        "nudity",
        "female nudity",
        "male nudity",
        "police",
        "suicide",
        "drugs",
        "alcohol",
        "money",
        "gun",
        "shootout",
        "explosion",
        "chase",
        "fight",
        "escape",
        "hospital",
        "doctor",
        "lawyer",
        "journalist",
        "small town",
        "duty",
        "lgbt",
        "1970s",
        "1980s",
        "1990s",
        "2000s",
    }
)


def is_telling_keyword(name: str | None) -> bool:
    return bool(name) and name.strip().lower() not in META_KEYWORDS | BROAD_KEYWORDS
