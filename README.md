# Watchly

<div align="center">

<!-- Premium Badge Collection -->
[![Version](https://img.shields.io/github/v/release/timilsinabimal/watchly?style=for-the-badge&logo=semver&color=6366f1)](https://github.com/timilsinabimal/watchly/releases)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/timilsinabimal/watchly?style=for-the-badge&color=f59e0b&logo=github)](https://github.com/timilsinabimal/watchly/stargazers)

</div>
<br/>

**Watchly** is a Stremio catalog addon that fills your Stremio home with personalized movie and series recommendations built from your own watch history. It reads what you've watched, rated, and loved — from **Stremio, Trakt, Simkl, MDBList, or Nuvio** — builds a numerical taste profile from it, and serves a set of recommendation rows ("Top Picks for You", "Because you watched …", genre and keyword catalogs, and more) using metadata from [TMDB](https://www.themoviedb.org/).

Everything is configured through a web page; you paste the resulting manifest URL into Stremio once, and the catalogs keep refreshing in the background.

## Watchly C

This is **Watchly C**, a personal fork of [Watchly](https://github.com/TimilsinaBimal/Watchly). It looks and works like stock Watchly; the differences are under the hood, each one in its own module in `app/watchly_c/` and each one switched off by an environment variable to get stock behaviour back.

| Tweak | What it does | Switch (default) |
| --- | --- | --- |
| Nuvio + MDBList ratings | With Nuvio as the history source and MDBList also connected, MDBList ratings (and when they were given) are laid over the Nuvio history, so titles can be *loved* and *liked*, not only *watched*. Titles rated on MDBList but never played in Nuvio join the library. If MDBList is down, Nuvio is used alone. | `NUVIO_MDBLIST_RATINGS` (`True`) |
| Weighted daily seeds | "Because you watched/loved" seeds are picked once a day, weighted toward titles rated 7+ in the last 6 months and how recently they were watched. The window widens when that pool is thin. Without ratings, the weight is recency, completion and rewatches. A seed isn't reused for about a week. | `WEIGHTED_ITEM_SEEDS` (`True`) |
| Mixed rows | **Rows** on the configure page is the total across movies and series (default 4). Each row mixes both: the seed's own type from the stock item engine, and every third card the other type, matched on the seed's keywords and genres. | `MIXED_ITEM_ROWS` (`True`) |
| Full rows for big histories | Stock Watchly drops watched titles by TMDB id before cutting a row to length, and by IMDb id only after. External histories carry IMDb ids, so a heavy watcher's rows were cut first and then emptied (a row of 3). Watched IMDb ids are now resolved to TMDB ids once, kept in a shared Redis map, and dropped before the cut. | `WATCHED_TMDB_IDS` (`True`) |
| Fixed row slots | The manifest always declares every theme slot (3 per type), every seasonal slot (3 per type) and every mixed slot (the Rows count), even on a day a build comes up short; an unfilled slot serves an empty row, which Nuvio hides. Nuvio puts any row it hasn't saved a position for at the bottom of the home screen, so a slot that came and went used to return there. | `FIXED_ROW_SLOTS` (`True`) |
| Timed shuffle | Rows with **Shuffle** on keep one order for an hour, then reshuffle, rather than reshuffling per request (which clients never showed, since they cache rows). Theme and item rows now honour the switch too. | `SHUFFLE_INTERVAL_HOURS` (`1`; `0` = stock) |
| Daily refresh | A Vercel cron (`vercel.json`, 04:00 UTC / 05:00 Lagos) calls `/watchly-c/daily-refresh`, which refetches every account's library (stock Watchly only refetches Stremio libraries), picks the day's seeds and rebuilds every row. Runs once a day. Set `CRON_SECRET` in Vercel to lock the endpoint to the cron. | `CRON_SECRET` (unset) |
| Titles filled in | Nuvio's progress rows carry no title, so a title finished there arrived nameless and a "Because you watched" row seeded from it read "Because you watched this title". Missing titles are now looked up on TMDB when the history is built. | `FILL_MISSING_TITLES` (`True`) |
| Daily "Watch it again" | The row picks 20 from the 80 best-fitting watched titles (stock: 40), and the daily refresh rebuilds it, so each day brings a new set instead of the same one a visit late. | `DAILY_REWATCH` (`True`) |
| Re-roll on Refresh | The dashboard Refresh button draws new seeds for the day (the old ones stay out for a week) instead of rebuilding around the same four. | `REROLL_SEEDS_ON_REFRESH` (`True`) |
| Ranked "Because you" rows | Item rows are scored against the seed (shared genres, lead genre, language, era, TMDB's recommended/similar rank, and the seed's own keywords) and scaled by quality, so a close match at 7.2 beats a loose one at 8.5. Anything under a weighted 6.5 is dropped (7.0 for seeds rated 8+), as are animation, documentary and family titles the seed doesn't share and titles in a language other than English or the seed's own. Cross-type cards must share one of the seed's two leading genres. A short row widens by best-rated titles in the seed's genres. Rows are trimmed to about 25. | `RELEVANCE_RANKING` (`True`) |
| Quality Top Picks | Top Picks drops anything under a weighted 6.8 (200+ votes), ranks the rest by taste fit scaled by quality (6.0 to 8.5 band), and asks TMDB discover for the best-rated titles instead of the most popular. The stock boost for very popular titles is gone. | `QUALITY_TOP_PICKS` (`True`) |
| Digital releases only | A movie from the last year is shown only once it has a digital or physical release in any country, so nothing still in cinemas turns up in a row. Older films are assumed available, as TMDB often lacks a digital date for them. A series must have started airing. | `DIGITAL_RELEASE_ONLY` (`True`) |
| Wider genre and keyword rows | Rows is the total across movies and series (2 to 10, default 6). Every row is declared under one type and blends both, and no genre or keyword anchors two rows. Row titles are written lines ("Revenge, Served Cold", "Edge-of-Your-Seat Thrillers"); only keywords with a title line become rows, so tags like `duringcreditsstinger` and `woman director` never do. Theme rows are held to the same floor and topped up to a full row: the keyword joined by your best keywords, then the anchors alone, then the leading genre, always keeping the country and era that define the row. | `WIDE_THEME_ROWS` (`True`) |
| Seasonal & contextual rows | Selectable on the configure page as "Seasonal & Contextual", with a Rows control for the total across movies and series (2 to 10, default 6). Every row is declared under one type and blends both where it can, and none repeats. The first is always On This Day: titles that opened in US cinemas on today's date in any year since 1960, widening by a day either side only while the exact day runs short. The rest cycle between one that follows the time of year (Spooky Season, Spooky Not Scary, African Stories in October, Holiday Season, Valentine's, Awards Season, Summer Blockbusters), the moment (a Friday or Saturday night, Sunday Slow, Rainy Day Comfort from an Open-Meteo forecast), and a rotating pick from Late Night Watch, Hidden Gems (older than two years and not already popular), a Time Machine decade, Short Binges and "From the People Behind" a movie you watched. The least recently shown comes first, so each gets its turn. These rows ask TMDB for English-language titles, except African Stories. A day's picks are fixed in Redis. Weather is one request a day for all accounts. | `CONTEXT_ROWS` (`True`), `WEATHER_LATITUDE`/`WEATHER_LONGITUDE` (Lagos) |
| No repeats across rows | Rows that claim first are built first (Because you rows, then seasonal, then genre and keyword, then the rest) and each row leaves out titles an earlier one shows. Claims are one small key per row, read once at build time; nothing is added to a cache hit. "Watch it again" is exempt. Other addons are not considered. | `CROSS_ROW_DEDUPE` (`True`) |
| Retired "Based on what you loved/liked" | Top Picks already draws on your whole taste profile and the "Because you" rows cover your loved titles one by one, so the two all-loved/all-liked rows only repeated them. They no longer appear on the configure page or in the manifest. | `RETIRE_ALL_BASED` (`True`) |
| Release years from 1950 | The release-year slider starts at 1950 (stock: 1970), and the decade Time Machine row reaches back to it. | `DEFAULT_YEAR_MIN` in `app/core/settings.py` |
| One row for movies and series | Top Picks, From your favourite Creators and Watch it again are each a single row declared under one type, alternating movies and series card by card, instead of a movie row and a series row. With every row single-type, the "Movies and series order" setting has nothing to order and is hidden. | `MIXED_BASE_ROWS` (`True`) |
| Gemini (or other LLM) key | Used only to write the titles of genre and keyword rows, from each row's filters, falling back to the written title if the model fails. It no longer designs the rows, since it can't honour a row count or keep two rows from sharing a genre. | `WIDE_THEME_ROWS` (`True`) |
| Setup on Vercel | Vercel freezes a function once it has answered, so the stock setup warm-up (run behind the save response) stalls at "Saving your configuration". On Vercel the configure page's status poll runs it instead. | `INLINE_WARMUP` (on when Vercel sets `VERCEL=1`) |
| Upstream sync check | `.github/workflows/upstream-sync.yml` checks weekly for new upstream commits, merges them into an `upstream-sync` branch and runs the tests there. Merge that branch into `main` to update. | Actions tab |

`DAILY_UTC_OFFSET_HOURS` (default `1`, Lagos) sets the local time used for daily seeds and shuffle windows.

**Two Nuvio profiles.** Set each profile up separately on the configure page (not "Set up every profile", which shares one set of keys): profile 1 with Nuvio and MDBList connected and Nuvio as the source; profile 2 with Nuvio alone.

## Table of contents

- [Watchly C](#watchly-c)
- [Features](#features)
- [How it works](#how-it-works)
- [Catalogs](#catalogs)
- [Watch history sources](#watch-history-sources)
- [Personalization](#personalization)
- [Screenshots](#screenshots)
- [Installation (Docker)](#installation-docker)
- [Unraid](#unraid)
- [Choose a Redis](#choose-a-redis)
- [Deploy on Vercel](#deploy-on-vercel)
- [Configuration reference](#configuration-reference)
- [Optional integrations](#optional-integrations)
- [Development](#development)
- [Architecture](#architecture)
- [Contributing](#contributing)
- [Funding & support](#funding--support)
- [Acknowledgements](#acknowledgements)

## Features

- **Personalized recommendations** — a taste profile (top genres, keywords, directors, cast, eras, countries, runtime) is built from your history and drives every catalog row.
- **Five history sources** — use your **Stremio** library, or your **Trakt**, **Simkl**, **MDBList** or **Nuvio** account. Ratings, watches, loves, and rewatches are all understood.
- **Multiple catalog types** — Top Picks, "Because you watched/loved", dynamic genre & keyword rows, recommendations from your recurring directors and actors, and "based on everything you loved/liked".
- **Fine-grained personalization** — discovery style (mainstream → hidden gems), release-year window, excluded genres (separately for movies and series), display language, and per-catalog enable/rename/shuffle controls.
- **Poster ratings overlay** — optionally overlay IMDb/TMDb-style ratings on posters via [RatingPosterDB](https://ratingposterdb.com/), Top Posters, or a custom template.
- **Bring your own keys** — supply your own TMDB, Simkl, or Gemini API keys, or rely on the server's.
- **Secure by design** — credentials are encrypted at rest in Redis and never appear in the manifest URL, which carries only a short opaque token.
- **Background sync** — catalogs are refreshed on a schedule so your home page is ready before you open it.
- **Fast** — aggressive Redis caching of profiles, libraries, and rendered catalogs keeps responses quick.

## How it works

1. You open the `/configure` page and connect a history source (Stremio login, Trakt/Simkl via OAuth, or an MDBList API key). Your credentials are encrypted and stored in Redis under a short opaque **token**. That token is embedded in your personal manifest URL.
2. Watchly fetches your watch history from the configured source and converts it into a source-agnostic library — ratings ≥ 9 count as *loved*, 7–8.9 as *liked*, the rest as *watched*.
3. From that library it builds a **taste profile**: a numerical fingerprint of your preferences across genres, keywords, people, eras, countries, and runtime.
4. When Stremio requests a catalog, Watchly routes the request to the matching recommendation engine, pulls candidates from TMDB (and Simkl where available), scores them against your profile, caps them for diversity, enriches them with metadata and (optionally) poster ratings, translates titles to your language, and returns a standard Stremio catalog.

Each user's state is keyed entirely on their token. You can switch history sources at any time; the library and profile are rebuilt from the new source.

## Catalogs

You choose which of these to enable on the configure page. Each can be toggled per content type (movies / series), renamed, hidden from the home page, or shuffled.

| Catalog | ID | What it shows |
| --- | --- | --- |
| **Top Picks for You** | `watchly.rec` | Your strongest personalized recommendations, combining profile-driven TMDB discovery with picks seeded from your library. |
| **Because you watched / loved** | `watchly.item` | Titles similar to one recent item. The seed is chosen at random from your 3 most-recent loved + 3 most-recent watched items; the row title becomes "Because you loved *X*" or "Because you watched *X*" accordingly. |
| **Genre & Keyword Catalogs** | `watchly.theme` | Dynamic, Netflix-style rows built from your favorite genres, keywords, and countries (e.g. "American Horror", "Based on a Novel"). Up to ~4 rows each for movies and series, varying with your history. |
| **From your favourite Creators** | `watchly.creators` | Recommendations from directors and lead actors who recur across multiple items in your library — not one-offs. |
| **Based on what you loved** | `watchly.all.loved` | Recommendations drawn from your entire set of loved items. |
| **Based on what you liked** | `watchly.liked.all` | Recommendations drawn from your entire set of liked items. |

## Watch history sources

Watchly works for users who keep their library in different places. Pick one source per install:

- **Stremio** — uses your Stremio library directly (requires a Stremio email/password or auth key).
- **Trakt** — connect via OAuth on the configure page; Watchly reads your watched history and ratings.
- **Simkl** — connect via OAuth on the configure page; Watchly reads your watched history and ratings.
- **MDBList** — paste your API key from [mdblist.com/preferences](https://mdblist.com/preferences/) on the configure page; Watchly reads your watched history and ratings. Needs no server-side setup.
- **Nuvio** — sign in to Nuvio on the configure page (the password goes from your browser to Nuvio, not to Watchly); Watchly reads the watched list of the profile you choose, one Watchly instance per profile if you want. Needs no server-side setup.

A single install uses exactly one source at a time. Switching sources rebuilds your library and profile from the new account.

## Personalization

All of these are set on the configure page and stored with your token:

- **Discovery style** — `mainstream`, `balanced`, `gems` (highly rated, less popular), or `all`. Controls the quality/popularity band candidates must fall in.
- **Release-year range** — restrict recommendations to a year window (default 1970–present).
- **Excluded genres** — hide genres you don't want, configured separately for movies and series.
- **Display language** — catalog titles and metadata are translated to your chosen language.
- **Sorting order** — show movies first, series first, or the default interleaving.
- **Poster ratings** — overlay ratings on posters via RPDB, Top Posters, or a custom URL template.
- **Per-catalog controls** — enable/disable, movie-only or series-only, hide from home, shuffle, and rename most catalogs.

## Screenshots

<img src="./app/static/screenshots/homepage.png" alt="Top Picks" width="800"/>

Find more screenshots [here](./app/static/screenshots/).

## Installation (Docker)

Docker is the recommended way to self-host. Watchly requires a **Redis** instance; a server **TMDB API key** is optional but recommended.

1. **Create a `docker-compose.yml`:**

   ```yaml
   services:
     redis:
       image: redis:7-alpine
       container_name: watchly-redis
       restart: unless-stopped
       volumes:
         - redis_data:/data

     watchly:
       image: ghcr.io/timilsinabimal/watchly:latest
       container_name: watchly
       restart: unless-stopped
       ports:
         - "8000:8000"
       env_file:
         - .env
       depends_on:
         - redis

   volumes:
     redis_data:
   ```

2. **Create a `.env` file** (see the [configuration reference](#configuration-reference) for all options):

   ```env
   # Required
   TOKEN_SALT=generate_a_long_random_secret
   HOST_NAME=https://your-public-addon-url

   # Optional, recommended
   TMDB_API_KEY=your_tmdb_api_key_here

   # Redis (matches the compose service name)
   REDIS_URL=redis://redis:6379/0

   # Optional — enable Trakt / Simkl (see "Optional integrations")
   # TRAKT_CLIENT_ID=
   # TRAKT_CLIENT_SECRET=
   # SIMKL_CLIENT_ID=
   # SIMKL_CLIENT_SECRET=
   ```

   `HOST_NAME` must be the public URL where the addon is reachable. It is used to build OAuth callback URLs and the manifest URL, so it has to match what Stremio (and Trakt/Simkl) will see.

3. **Start it:**

   ```bash
   docker-compose up -d
   ```

4. **Configure and install:**
   Open `http://localhost:8000/configure` (or your `HOST_NAME`), connect a history source, pick your catalogs, and paste the generated manifest URL into Stremio.

### Unraid

A Community Applications-style template lives at [`unraid/watchly.xml`](unraid/watchly.xml).

1. Install **Redis** from Community Applications (any Redis container works).
2. On the **Docker** tab, click **Add Container**, switch to advanced view, and set the template URL to
   `https://raw.githubusercontent.com/TimilsinaBimal/Watchly/main/unraid/watchly.xml` — or add
   `https://github.com/TimilsinaBimal/Watchly` under *Template Repositories* and pick **Watchly** from the template list.
3. Fill in the required fields: TMDB API key, a long random token salt, the Redis URL from step 1
   (e.g. `redis://YOUR-UNRAID-IP:6379/0`), and the host name your Stremio clients will reach the addon on.
4. Start the container and open the WebUI to configure your catalogs.

## Choose a Redis

Watchly keeps every account, cache and rendered row in Redis, so it needs one it can reach on every request.

- **Docker: run Redis next to Watchly.** The `redis:7-alpine` service in the compose file above costs nothing, has no command or connection limits, and never gets deleted for being idle.
- **Vercel: use [Upstash](https://upstash.com/pricing/redis).** Its free plan fits a small instance, TLS is always on, and a free database that sits idle is archived and can be restored rather than deleted.

Roughly how much a user costs: serving a cached row takes about 3 commands (alias lookup, warm-up check, catalog read), so a home screen of ~10 rows plus the manifest is ~35. Each row is rebuilt once a day, about 10 more commands per row. A user who opens Stremio a few times a day comes to about 150–250 commands, or 5–8K a month.

| | Upstash free | Redis Cloud free (Deploy button) | Docker Redis |
| --- | --- | --- | --- |
| Commands | 500K / month (~60–100 users) | 100 ops/sec | no limit |
| Data | 256 MB | 30 MB | your disk |
| Connections | not published | 30 | no limit |
| TLS | always on (`rediss://`) | not available on free | local network |
| Idle database | archived after 30 days, restorable | deleted after 14 days without commands | never |
| Next step up | pay as you go, $0.20 per 100K commands | paid plans from $5 / month | — |

Sources: [Upstash pricing](https://upstash.com/docs/redis/overall/pricing), [Upstash FAQ](https://upstash.com/docs/redis/help/faq), [Redis Cloud Essentials limits](https://redis.io/docs/latest/operate/rc/subscriptions/view-essentials-subscription/essentials-plan-details/), [Redis Cloud TLS](https://redis.io/docs/latest/operate/rc/security/database-security/tls-ssl/), [Redis pricing](https://redis.io/pricing/). Checked September 2026; confirm current limits before you pick.

Redis Cloud's free plan is the weaker fit on Vercel: each function instance opens up to `REDIS_MAX_CONNECTIONS` (20) connections, so two instances can exhaust its 30, and a home screen requests every row at once, which a few users together can push past 100 ops/sec. It is fine for trying Watchly out; switch to Upstash before you share the instance.

### Set up Upstash

1. In the [Upstash console](https://console.upstash.com), create a Redis database. Pick the region nearest your Vercel functions; new Vercel projects run in `iad1` (Washington, D.C.), so AWS `us-east-1` is the usual choice.
2. Open the database's **Connect** section and copy the `rediss://default:<password>@<host>:<port>` URL from the redis-cli snippet. Don't use the REST URL and token (`UPSTASH_REDIS_REST_*`, `KV_REST_API_*`): they are for Upstash's HTTP API, which Watchly's Redis client can't speak.
3. In Vercel, open **Settings → Environment Variables** and set `REDIS_URL` to that URL.
4. Redeploy from the **Deployments** tab.

The `rediss://` scheme turns on TLS, and redis-py handles it with no extra settings.

### Redis Cloud (Deploy button)

The Deploy button's **Redis** store creates a free Redis Cloud database and adds `REDIS_URL` to the project for you. On the free plan, set `REDIS_MAX_CONNECTIONS=10` so two instances stay under its 30-connection limit, and remember that the database is deleted after 14 days with no commands.

## Deploy on Vercel

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FTimilsinaBimal%2FWatchly&project-name=watchly&repository-name=watchly&env=TOKEN_SALT,HOST_NAME&envDescription=A%20long%20random%20TOKEN_SALT%20and%20HOST_NAME%20set%20to%20https%3A%2F%2F%3Cproject-name%3E.vercel.app.%20TMDB_API_KEY%20is%20optional%20but%20recommended%3B%20add%20it%20later%20under%20Settings%20%3E%20Environment%20Variables.&envLink=https%3A%2F%2Fgithub.com%2FTimilsinaBimal%2FWatchly%23deploy-on-vercel&stores=%5B%7B%22type%22%3A%22integration%22%2C%22integrationSlug%22%3A%22redis%22%2C%22productSlug%22%3A%22redis%22%7D%5D)

Vercel detects the FastAPI app with no extra config and runs it on Python 3.12, the version `pyproject.toml` and `.python-version` pin. A running instance also serves this guide at `/self-host`.

1. Click the button and pick a project name.
2. Accept the **Redis** store to try Watchly on a free Redis Cloud database; it adds `REDIS_URL` to the project. For an instance you'll keep, skip it and [set up Upstash](#set-up-upstash) instead (see [Choose a Redis](#choose-a-redis)).
3. Fill in `TOKEN_SALT` (for example `openssl rand -hex 32`) and `HOST_NAME` (`https://<project-name>.vercel.app`), then deploy. `TMDB_API_KEY` is optional but recommended: add it under **Settings → Environment Variables** (see [Configuration reference](#configuration-reference)).
4. If the production domain Vercel assigned differs from your `HOST_NAME`, fix it under **Settings → Environment Variables**.
5. Redeploy from the **Deployments** tab; environment variable changes only apply to new deployments.
6. Open `https://<your-domain>/configure` and set up your catalogs.

Leave `APP_ENV` unset: the `production` default refuses to start with the placeholder `TOKEN_SALT`.

**Limits.** Watchly finishes some work after it has answered a request, and Vercel's Python runtime has no way to keep the function alive for it, so that work may be paused or cut short:

- The setup warm-up may not finish. The configure page falls back to "Setup is taking a while", and the first home screen builds its rows as Stremio requests them, so it is slower.
- A stale row is served while it rebuilds in the background; if the rebuild is cut, the next request tries again.
- There is no scheduler: catalogs refresh only when Stremio requests them, and that refresh is not guaranteed to complete.
- A request can run for 5 minutes by default, so a very large library may time out on its first build.

If you want background refreshes to run reliably, use Docker.

## Configuration reference

All settings are environment variables. Only `TOKEN_SALT` and `HOST_NAME` are strictly required, plus a reachable Redis.

### Required

| Variable | Description |
| --- | --- |
| `TMDB_API_KEY` | Optional, recommended. Each user enters their own TMDB key on the configure page; this server key is the fallback. Without it the configure page offers English only, and accounts saved before the page asked for a key have none to use. |
| `TOKEN_SALT` | Secret used to derive the encryption key for stored credentials. **Set a long random value** — the default `change-me` is insecure. |
| `HOST_NAME` | Public base URL of the addon (used for manifest and OAuth callback URLs). |

### Redis

| Variable | Default | Description |
| --- | --- | --- |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection URL. Redis is required. |
| `REDIS_MAX_CONNECTIONS` | `20` | Max Redis connections per process. |
| `REDIS_CONNECTIONS_THRESHOLD` | `100` | Background Redis-heavy jobs back off above this many total clients. |
| `REDIS_TOKEN_KEY` | `watchly:token:` | Key prefix for stored user tokens. |

### Optional integrations

| Variable | Default | Description |
| --- | --- | --- |
| `TRAKT_CLIENT_ID` / `TRAKT_CLIENT_SECRET` | — | Trakt OAuth app credentials; enable Trakt as a history source. |
| `SIMKL_CLIENT_ID` / `SIMKL_CLIENT_SECRET` | — | Simkl OAuth app credentials; enable Simkl as a history source. |

### Tuning & behavior

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8000` | HTTP port. |
| `ADDON_ID` | `com.bimal.watchly` | Stremio addon ID in the manifest. |
| `ADDON_NAME` | `Watchly` | Display name in the manifest. |
| `APP_ENV` | `production` | `development`, `production`, or `vercel`. |
| `TOKEN_TTL_SECONDS` | `0` | Token expiry in seconds; `0` = never expire. |
| `AUTO_UPDATE_CATALOGS` | `true` | Refresh dynamic catalogs in the background on a schedule. |
| `CATALOG_REFRESH_INTERVAL_SECONDS` | `86400` | Background refresh interval (24 h). |
| `CATALOG_CACHE_TTL` | `60` | `Cache-Control` max-age, in seconds, that Stremio may reuse a catalog response for. |
| `CATALOG_STALE_TTL` | `604800` | Soft-expiration fallback for cached catalogs (7 d). |
| `RECOMMENDATION_SOURCE_ITEMS_LIMIT` | `10` | Number of library items used to seed recommendations. |
| `LIBRARY_ITEMS_LIMIT` | `20` | Library item cap used in parts of the pipeline. |
| `ANNOUNCEMENT_HTML` | `""` | Optional HTML banner shown on the configure page. |
| `ALLOW_SIGNUPS` | `true` | Set to `false` to lock the instance to existing accounts; new signups are rejected. |

## Optional integrations

These are only needed if you want the corresponding feature; Watchly runs fine with just TMDB + Redis.

- **Trakt** — create an API app at [trakt.tv/oauth/applications](https://trakt.tv/oauth/applications). Set the redirect URI to `HOST_NAME/auth/trakt/callback` and put the client ID/secret in `TRAKT_CLIENT_ID` / `TRAKT_CLIENT_SECRET`.
- **Simkl** — create an app at [simkl.com/settings/developer](https://simkl.com/settings/developer). Set the redirect URI to `HOST_NAME/auth/simkl/callback` and put the credentials in `SIMKL_CLIENT_ID` / `SIMKL_CLIENT_SECRET`.
- **AI-named rows** — users configure an LLM provider (Gemini, OpenAI, Anthropic, or OpenRouter) with their own API key on the configure page; no server config required. Without one, rows fall back to deterministic names.
- **Poster ratings (RPDB)** — users enter their own [RatingPosterDB](https://ratingposterdb.com/) key on the configure page; no server config required.

## Development

Dependencies are managed with [uv](https://github.com/astral-sh/uv); a `requirements.txt` is kept in sync for non-uv environments. **Python 3.12+** is required, and a running Redis is needed for most functionality.

```bash
# Clone
git clone https://github.com/TimilsinaBimal/Watchly.git
cd Watchly

# Install dependencies
uv sync

# Run the dev server (auto-reload)
uv run main.py --dev
# or
uvicorn app.core.app:app --reload
```

Create a `.env` with at least `TMDB_API_KEY`, `TOKEN_SALT`, `HOST_NAME`, and `REDIS_URL` before running. The configure UI is served at `/configure`.

### Tests, linting, formatting

```bash
# Tests (pytest is installed into the venv on demand)
PYTHONPATH=. uv run --with pytest pytest tests/
PYTHONPATH=. uv run --with pytest pytest tests/test_catalog_endpoint.py -v

# Lint / format (also enforced by pre-commit on commit)
pre-commit run --all-files
black .      # line length 120, py312
isort .      # black profile
flake8 .     # config in setup.cfg
```

## Architecture

Watchly is a FastAPI service that speaks the Stremio addon protocol. The request flow for every authenticated endpoint is:

1. **`app/services/context.py:load_user_context`** — decrypts the token, parses settings, resolves auth, and builds the `LibraryCollection` from the configured `watch_history_source`.
2. **`app/services/recommendation/catalog_service.py`** — routes the catalog ID to a recommendation engine (Top Picks, theme, item, creators, all-loved/liked).
3. The engine's results pass through metadata enrichment, poster-ratings overlay, translation, and serialization into a Stremio catalog.

The taste-profile pipeline lives in `app/services/profile/`, all external HTTP goes through `app/core/base_client.py:BaseClient` (retries, timeouts, structured errors), and Redis (`app/services/user_cache.py`, `app/services/redis_service.py`) is the source of truth for user state.

### Project layout

```
app/
  api/            # FastAPI routers: manifest, catalog, tokens, oauth, dashboard, validation, health …
  core/           # app setup, config/settings, base HTTP client, security, constants
  models/         # Pydantic models (library, profile, …)
  services/
    profile/      # taste-profile builder
    recommendation/ # catalog routing + per-type engines, scoring, diversity, filtering
    poster_ratings/ # RPDB / Top Posters / custom overlays
    stremio/      # Stremio library + auth
    tmdb/         # TMDB client, genres, countries
    …             # trakt, simkl, gemini, translation, caching, catalog updater
  static/, templates/  # the configure UI and dashboard
```

### Key endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /configure` | Web UI for setup and catalog selection. |
| `GET /{token}/manifest.json` | Per-user Stremio manifest. |
| `GET /{token}/catalog/{type}/{id}.json` | Catalog data for a content type and catalog ID. |
| `POST /tokens/` | Create a token from submitted credentials/settings. |
| `GET /auth/trakt`, `GET /auth/simkl` | OAuth start; `/callback` variants complete the flow. |
| `GET /{token}/dashboard/data` | User dashboard data. |
| `GET /health`, `GET /stats` | Readiness probe and usage stats. |

## Contributing

Contributions of all sizes are welcome!

- **Small bug fixes & improvements** — open a Pull Request directly.
- **Major features & refactors** — please [open an issue](https://github.com/TimilsinaBimal/Watchly/issues) first to discuss the approach. This keeps your work aligned with the project's direction and saves you time.

## Funding & support

If you find Watchly useful, please consider supporting the project:
- [Buy me Mo:Mo](https://buymemomo.com/timilsinabimal)

## Bug reports

Found a bug or have a feature request? Please [open an issue](https://github.com/TimilsinaBimal/Watchly/issues) on GitHub.

## Contributors

Thank you to everyone who has contributed to the project!

<a href="https://github.com/TimilsinaBimal/watchly/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=TimilsinaBimal/watchly" />
</a>

## Acknowledgements

Special thanks to **[The Movie Database (TMDB)](https://www.themoviedb.org/)** for the rich metadata that powers Watchly's recommendations, and to **Trakt** and **Simkl** for their history APIs.
