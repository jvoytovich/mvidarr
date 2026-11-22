---
layout: page
title: Usenet, Plex Music Videos and Chart Lists
---

# Usenet first, a Plex Music Videos library, and chart lists

This fork adds the parts that make MVidarr behave like the other *arr apps:
it looks for a music video on your Usenet and torrent indexers before it
touches YouTube, files everything into a Plex "Music Videos" library, and
can collect whole chart lists such as the Billboard Year-End Hot 100.

Everything here is off until you turn it on in **Settings > Services**.

## How a wanted video is fetched

1. **Prowlarr** is searched once for `<main artist> <title>` in category
   3020 (Audio/Video, where scene music video releases live).
2. Each release is scored. It must name the main artist and the core title
   as whole words. Live, unplugged, compilation, karaoke, lyric, audio-only,
   remix and full-DVD releases are rejected unless the song title itself has
   that word. Anything with more than a few unexplained extra words is
   rejected too. Usenet scores above torrents, and resolution counts
   (2160p > 1080p > 720p > DVD rip).
3. The best release goes to **SABnzbd** (Usenet) or **qBittorrent**
   (torrents, only when no Usenet release fits). MVidarr downloads the NZB or
   torrent through Prowlarr itself and uploads it, so the download client
   never needs to reach Prowlarr.
4. A poll task (every 2 minutes) watches the job. When it finishes, the
   largest video file is moved (Usenet) or hard-linked (torrents, so seeding
   continues) into the library, and Plex is asked to scan that one folder.
5. If the job fails or stalls for 24 hours, the next-best release is tried
   (2 releases per video by default). When none is left, the video goes back
   to WANTED and the next download run uses **YouTube**.

Searches are budgeted (`indexer_max_searches_per_day`, default 150) and a
video is not searched again for 14 days. An HTTP 429 from Prowlarr stops
searching until the next UTC day.

### YouTube, the fallback

- The upload is chosen with YouTube Music search, which marks label uploads
  as official music videos. Without it, a plain yt-dlp search is scored on
  the channel name. Fan uploads and TV clips are used only when there is no
  official video, and they are flagged `unofficial_clip` for review.
- Age-restricted uploads ("Sign in to confirm your age") are a per-video
  problem: MVidarr tries another upload of the same video and otherwise marks
  that one video failed.
- A bot check or HTTP 429 pauses **all** YouTube downloads for
  `youtube_cooldown_hours` (default 6) and puts the affected videos back to
  WANTED. The Lists page shows the pause and has a button to resume.
- At most `max_concurrent_downloads` YouTube downloads run at once.

### Upgrades

Once a day, up to 25 videos that came from YouTube (or were imported from
disk) are searched on the indexers. A release replaces the file when its
resolution is higher, or when the current copy is an unofficial clip.

## Library layout

New downloads are saved as

```
<music_videos_path>/<Artist>/<Artist> - <Title>.<ext>
<music_videos_path>/<Artist>/<Artist> - <Title>.jpg   (poster for Plex)
```

Featured artists stay in the file name but not the folder
(`Dua Lipa/Dua Lipa featuring DaBaby - Levitating.mp4`). Set
`download_filename_format` to `legacy` for the original `Artist/Title.ext`.

### Importing files you already have

`POST /api/arr/library/import` with `{"path": "...", "dry_run": true}`
(admin only) walks a folder and registers every video file in place. Nothing
is moved or renamed. Artist and title come from `Artist - Title.ext`, or from
the parent folder when the name has no ` - `. A leading track number
(`37. Samantha Fox - Touch Me`) and release tags after the title
(`Take On Me.540p.h264-rage`) are dropped. Hidden folders and NAS folders
starting with `@` or `#` (`@eaDir`, `@Recently-Snapshot`, `#recycle`) are
skipped; Plex skips hidden folders too, so new downloads never start a file
or folder name with a dot. A file that matches a WANTED
video fills it, so it will not be downloaded again. New artists start
unmonitored with auto-download off. Run it with `dry_run: false` once the
counts look right.

## Plex Music Videos library

Use a **Movies**-type library with the **Personal Media** agent and local
assets enabled. Set the Plex URL and token, the library name (or section
id), and a path mapping if Plex sees the folder under a different path
(`/media/0 Music Videos=/mnt/blofeld/music-videos`).

Every 30 minutes (or **Sync Library Now**):

- Items are matched to downloaded videos by file path.
- Title is set to `Artist - Title`, sort title to `Artist Title`, year to the
  video's year or the first chart year, genres to at most two broad genres
  mapped from the artist's tags, and the summary lists chart positions.
- Collections (new ones appear only in the Collections tab, so the library
  grid stays a list of videos; change `plex_mv_collection_mode` to show them
  in the grid. A mode you change in Plex is kept):
  - one per chart list, e.g. `Billboard Year-End 1984`
  - `Genre: Rock`, `Genre: Hip-Hop`, ... (at least 3 videos). Artists
    without genres get them from MusicBrainz tags every 6 hours.
  - `Decade: 1980s`
  - `Artist: Madonna` for artists with at least 2 videos
  - `Unofficial clips (review)`
- Ordered playlists for ranked lists, e.g. `Billboard Year-End Hot 100: 1984`
  in chart order.

Collections starting with `Genre: `, `Decade: ` or `Artist: ` are managed by
MVidarr and removed when they no longer apply. Other collections are left
alone.

**Preview Changes** (or `POST /api/arr/plex/sync?dry_run=true`) reads Plex and
reports what a sync would edit, create, change and delete, without writing
anything. Run it before the first sync on a library that already has
collections.

## Chart lists

With **Billboard Year-End lists** on, a weekly task reads the Wikipedia
pages "Billboard Year-End Hot 100 singles of <year>" (1956 onward; a new year
appears in December). Each year is a list on the **Lists** page. Songs that
have no video yet become WANTED videos under unmonitored artists, so the
normal pipeline fills them, Usenet first.

### Taking over from the homelab Billboard script

`POST /api/arr/lists/billboard/import-state` (admin only) with
`{"state_db": ".../state.db", "lists_json": ".../lists.json"}` loads the
script's lists, registers its downloaded files in place (host `/mnt/media`
is mapped to `/media`), keeps its YouTube ids and unofficial flags, and
remembers the ids it rejected. Disable the script's timers afterwards.

## Settings reference

| Key | Default | What it does |
|---|---|---|
| `indexer_enabled` | false | Search Prowlarr before YouTube |
| `prowlarr_url`, `prowlarr_api_key` | | Prowlarr |
| `sabnzbd_url`, `sabnzbd_api_key`, `sabnzbd_category` | `musicvideos` | SABnzbd |
| `indexer_torrents_enabled` | true | Use qBittorrent when no Usenet release fits |
| `qbittorrent_url`, `_username`, `_password`, `_category` | `musicvideos` | qBittorrent |
| `indexer_path_mappings` | | `client path=MVidarr path;...` |
| `indexer_max_searches_per_day` | 150 | Search budget |
| `indexer_research_days` | 14 | Gap before a video is searched again |
| `indexer_min_resolution` | 480 | Lowest release resolution accepted |
| `indexer_max_grabs_per_video` | 2 | Releases tried before YouTube |
| `indexer_stall_hours` | 24 | Give up on a stuck job |
| `indexer_upgrades_enabled`, `indexer_upgrade_searches_per_day` | true, 25 | Upgrades |
| `plex_mv_enabled` | false | Manage the Plex Music Videos library |
| `plex_server_url`, `plex_token` | | Plex |
| `plex_mv_section_name` / `plex_mv_section_id` | `Music Videos` | Which library |
| `plex_mv_path_mappings` | | `MVidarr path=Plex path;...` |
| `plex_mv_collections_lists/genre/decade/artist` | true | Collection types |
| `plex_mv_artist_collection_min`, `plex_mv_genre_min_items` | 2, 3 | Collection thresholds |
| `plex_mv_unofficial_collection` | `Unofficial clips (review)` | Review collection |
| `artist_genres_from_musicbrainz` | true | Fill missing artist genres from MusicBrainz tags (every 6 hours, 200 artists a run) |
| `plex_mv_collection_mode` | `hide` | Where new collections appear: `hide`, `show_items`, `hide_items`, `default` |
| `billboard_lists_enabled`, `billboard_first_year` | false, 1956 | Chart lists |
| `lists_auto_add_wanted` | true | Create WANTED videos for list songs |
| `download_filename_format` | `artist_title` | File naming |
| `download_write_thumbnail` | true | Poster `.jpg` next to YouTube downloads |
| `youtube_cooldown_hours` | 6 | Pause after a bot check |

## Docker volumes

The container must see the library, SABnzbd's completed folder and
qBittorrent's save path. When they are mounted at the same paths the
download clients use, no `indexer_path_mappings` are needed, and torrents
can be hard-linked when the library is on the same filesystem.
