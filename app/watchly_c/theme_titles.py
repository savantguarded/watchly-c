from typing import Any

from app.services.tmdb.countries import COUNTRY_ADJECTIVES

# Row titles for the template (no-LLM) path. Only TMDB keywords with a line here become
# rows: most of the rest ("woman director", "violence", "death") make a bad title.
KEYWORD_LINES = {
    "revenge": "Revenge, Served Cold",
    "heist": "The Perfect Heist",
    "serial killer": "Inside a Killer's Mind",
    "murder": "Murder Most Foul",
    "time travel": "Time Benders",
    "time loop": "Stuck in a Loop",
    "dystopia": "Dystopian Futures",
    "post-apocalyptic future": "After the End of the World",
    "superhero": "Heroes in Capes and Cowls",
    "based on comic": "Straight Off the Comic Page",
    "gangster": "Gangsters and Their Empires",
    "mafia": "Family Business: The Mafia",
    "organized crime": "Crime Syndicates",
    "drug dealer": "Dealers and Kingpins",
    "undercover": "Deep Undercover",
    "assassin": "Assassins at Work",
    "hitman": "Hitmen with Rules",
    "spy": "Spies and Double Agents",
    "detective": "Detectives on the Case",
    "conspiracy": "Conspiracies Unravelling",
    "kidnapping": "Taken: Kidnappings Gone Wrong",
    "hostage": "Hostage Situations",
    "prison": "Life Behind Bars",
    "courtroom": "Days in Court",
    "survival": "Survive at All Costs",
    "vampire": "Fangs and Immortals",
    "zombie": "The Undead Rise",
    "ghost": "Ghost Stories",
    "haunted house": "Haunted Houses",
    "witch": "Witches and Hexes",
    "monster": "Monsters Unleashed",
    "alien": "We Are Not Alone",
    "robot": "Robots and Their Makers",
    "artificial intelligence (a.i.)": "When Machines Think",
    "space": "Lost in Space",
    "parallel universe": "Parallel Worlds",
    "amnesia": "Who Am I? Amnesia Stories",
    "twist ending": "Endings You Won't See Coming",
    "psychological thriller": "Mind Games",
    "dark comedy": "Dark Humour",
    "black comedy": "Dark Humour",
    "satire": "Sharp Satire",
    "parody": "Spoofs and Send-Ups",
    "buddy comedy": "Odd Couples and Buddies",
    "coming of age": "Coming of Age",
    "high school": "High School Years",
    "road trip": "Road Trip Rides",
    "small town": "Small Town Secrets",
    "based on novel or book": "From the Page to the Screen",
    "based on true story": "Based on True Events",
    "biography": "Real Lives, Told Well",
    "world war ii": "World War II Stories",
    "martial arts": "Martial Arts Mastery",
    "samurai": "Way of the Samurai",
    "musical": "Break Into Song",
    "dancing": "Dance Floor Dreams",
    "wedding": "Wedding Bells and Chaos",
    "christmas": "Christmas Cheer",
    "cult": "Inside the Cult",
    "con artist": "Con Artists and Grifters",
    "wall street": "Greed on Wall Street",
    "political intrigue": "Power Plays and Intrigue",
    "pirate": "Pirates and Plunder",
    "treasure hunt": "Treasure Hunts",
    "love triangle": "Love Triangles",
    "female protagonist": "Women in the Lead",
    "friendship": "Bonds of Friendship",
    "betrayal": "Betrayed",
    "obsession": "Dangerous Obsessions",
    "disaster": "Disaster Strikes",
    "virus": "Outbreak",
    "military": "Boots on the Ground",
    "terrorism": "Race Against Terror",
}

GENRE_LINES = {
    28: "Pulse-Pounding Action",
    12: "Grand Adventures",
    16: "Animation Worth a Look",
    35: "Laugh-Out-Loud Comedies",
    80: "Crime and Consequence",
    99: "Documentaries That Hook You",
    18: "Dramas That Linger",
    10751: "Family Night Picks",
    14: "Fantasy Worlds",
    36: "History, Retold",
    27: "Horror After Dark",
    10402: "Music on Screen",
    9648: "Mysteries to Unravel",
    10749: "Romance Front and Centre",
    878: "Science Fiction Frontiers",
    53: "Edge-of-Your-Seat Thrillers",
    10752: "War Stories",
    37: "Westerns, Dust and All",
    10759: "Action and Adventure Series",
    10765: "Sci-Fi and Fantasy Worlds",
    10768: "War and Power Plays",
}

GENRE_SHORT = {
    28: "Action",
    12: "Adventure",
    35: "Comedy",
    80: "Crime",
    18: "Drama",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    9648: "Mystery",
    10749: "Romance",
    878: "Sci-Fi",
    53: "Thriller",
    10752: "War",
    37: "Western",
    10759: "Action",
    10765: "Sci-Fi",
    10768: "War",
}


def title_for(axes: list[tuple[str, str, Any]], keyword_names: dict[int, str]) -> str:
    """A row's title from its axes: the keyword's line when it has one, otherwise the
    genres (and country) in plain words. Axes are (role, axis type, value)."""
    genres = [v for _, kind, v in axes if kind == "g"]
    keywords = [keyword_names.get(v) for _, kind, v in axes if kind == "k"]
    country = next((v for _, kind, v in axes if kind == "ct"), None)

    for name in keywords:
        if name and name.lower() in KEYWORD_LINES:
            return KEYWORD_LINES[name.lower()]
    first = genres[0] if genres else None
    if first in GENRE_SHORT and country in COUNTRY_ADJECTIVES:
        return f"{COUNTRY_ADJECTIVES[country][0]} {GENRE_SHORT[first]}"
    if first in GENRE_SHORT and len(genres) > 1 and genres[1] in GENRE_SHORT and genres[1] != first:
        return f"{GENRE_SHORT[first]} Meets {GENRE_SHORT[genres[1]]}"
    return GENRE_LINES.get(first, "Picked for You")
