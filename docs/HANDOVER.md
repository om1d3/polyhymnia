# Handover: Polyhymnia and the Jellyfin music library

Date of this handover: 26 November 2026.

This document gives the full state of the project. Use it to continue the work in a new conversation.

## 1. Instructions for the assistant

1. Write all communication in ASD-STE100 Simplified Technical English. Also use ASD-STE100 for all texts in the repository.
2. The user has ADHD. Give one clear, ordered procedure. Put one action in each step. Add a "Result:" line after each step that shows output.
3. Do not put comments (`# …`) in commands. The shell is zsh, and zsh does not accept comments in interactive commands.
4. The user often works in `screen` and cannot scroll. Thus, send long output to a file. Give a short command to show the important lines, for example `head -n 9` or `tail -n 2`.
5. To move files between the laptop and the server, use `/tmp` on the server. On the laptop, use `scp admin@10.30.0.12:/tmp/<file> .` On the server, use `mv /tmp/<file> .` as root.
6. Always give a dry run first. Then give the same command with `--commit`.
7. The assistant cannot connect to the server. The user runs all commands and uploads the results.
8. To edit new albums, use the tool `tools/editor/edit_names.py` in the repository (section 9). Then examine each change manually.

## 2. Environment

| Item | Value |
|---|---|
| Server | `bender`, TrueNAS SCALE, IP `10.30.0.12` |
| Login | SSH as `admin`, then `sudo -i` for root |
| Shell | zsh (with the `atuin` history tool) |
| Jellyfin | Version 10.11.11, Docker container, `http://localhost:8096` on bender |
| Forgejo | Docker container on bender. Web: `https://git.home.arpa`. SSH: port 2222. HTTP: port 3030. |
| Docker | Docker Compose plugin v2.38.1, git 2.39.5 |
| Main stack | `/mnt/BIG/filme/docker-compose` (a git repository with remote `git@github.com:om1d3/futurama-docker.git`) |

### Important rule for `screen` on TrueNAS

On TrueNAS, `sudo` controls the root shell with a seccomp filter. When the SSH session that started `sudo` closes, the root shell cannot start programs. All commands then fail with "function not implemented". Thus:

1. Connect with SSH as `admin`.
2. Start `screen` as `admin`: `screen -S jf`.
3. Use `sudo -i` in `screen`.
4. To return later, connect with SSH as `admin` and use `screen -r jf`. Do not use `sudo -i` first.

For long jobs, use `../jfa bg <command>`. Docker then runs the job without a shell.

## 3. Polyhymnia

Polyhymnia is a Python tool in Docker. It corrects the names, artists and covers in the Jellyfin music library. The name comes from the Muse of sacred song.

| Item | Value |
|---|---|
| Program folder | `/mnt/BIG/filme/docker-compose/polyhymnia` |
| Data folder | `/mnt/BIG/filme/docker-compose/polyhymnia/data` (ignored by git) |
| Settings | `polyhymnia/.env` (`JELLYFIN_URL`, `JELLYFIN_API_KEY`, `MB_CONTACT`). A copy is `data/jf.env`. |
| Docker image | `polyhymnia:local`. The wrapper `jfa` builds it automatically. |
| Version | 1.1. `polyhymnia.py` has 1857 lines, MD5 `4c135b493ed983bf51e7d8b3ebcb5528`. |
| Forgejo | `https://git.home.arpa/horia/polyhymnia` (remote `ssh://git@git.home.arpa:2222/horia/polyhymnia.git`) |
| GitHub | `https://github.com/om1d3/polyhymnia` (public). Forgejo sends each push to GitHub with a push mirror. |
| License | MIT, `Copyright © 2026 Horia` |

The main stack ignores the program folder: its `.gitignore` contains `polyhymnia/`. This `.gitignore` change is not committed yet in the main stack repository.

### Start a command

```zsh
cd /mnt/BIG/filme/docker-compose/polyhymnia/data
../jfa <command>
```

### Files in the repository

`.dockerignore`, `.env.example`, `.gitignore`, `Dockerfile`, `LICENSE`, `README.md`, `compose.yaml`, `jfa`, `polyhymnia.py`, `requirements.txt`, `docs/logo.png`, `docs/logo.svg`, `docs/cover-picker.png`, `docs/HANDOVER.md` (this document), `tools/editor/edit_names.py`, `tools/editor/rules.py`, `tools/editor/README.md`.

### Commits (newest first)

| Commit | Message |
|---|---|
| (next) | Add the handover document and the name editor |
| `040ce53` | Add the disclaimer and the MIT license |
| `ce6f7f9` | Add --new to export-names |
| `2e6ada8` | Add --search to covers-fetch, correct the picker text |
| `d6b9fd1` | Use ASD-STE100 in all texts |
| `2bdef53` | Rename to Polyhymnia, add logo and screenshot |
| `4902f34` | Fix clone address in README |
| `3b77ed2` | First version |

### Commands

| Command | Purpose |
|---|---|
| `export-names` | Write the names to JSON files. `--new`: only albums that are not in an edited file. `--album "text"`, `--split N`, `-o file`. |
| `import-names ORIGINAL EDITED` | Write the changes from an edited file to Jellyfin. It locks each changed name. |
| `check-names EDITED…` | Compare Jellyfin with edited files. `--fix` makes a plan to correct the differences. |
| `lock-names EDITED…` | Lock all names, so that a library scan cannot change them. |
| `covers-fetch` | Download the current covers and the new covers. It downloads only albums that it does not know. `--refetch`: all albums again. `--search`: ignore the MusicBrainz IDs in the files. |
| `covers-review` | Web page at `http://10.30.0.12:8765`. The user clicks the cover to use. Each click is saved in `covers/choices.json`. |
| `covers-apply` | Upload the selected covers. Files larger than 15 MB become JPEG. |
| `restore BACKUP` | Restore the initial values from a backup file. |
| `audit`, `apply` | The first method (CSV report against MusicBrainz). It is not used now. |

All commands that write data do a dry run without `--commit`. Before each write, Polyhymnia makes a backup file (`backup-<date>-<time>.json`).

### Message words

| Message | Definition |
|---|---|
| `NOT CHANGED: …` | Polyhymnia did not change this item. The line shows the reason. |
| `This was a dry run.` | No data changed. |
| `Result: N changed, N errors.` | The result of a write. |
| `All values are the same as in the edited files.` | `check-names` found no difference. |

## 4. State of the library

| Item | State |
|---|---|
| Albums and tracks | 1053 albums, 16479 tracks |
| Names | All album names, album artists, track titles and track artists are correct (`check-names` on 26 November 2026). |
| Locks | All names are locked (12,880 locks by `lock-names`, 4,652 locks before). |
| Artist pages | 850 correct |
| Covers | 342 selected covers are in Jellyfin. 336 are the same file. 6 were converted to JPEG. |
| Exception | "Dwarfland / Love theme" also has the artist David Lynch from the music files. This is correct. |

### Files in the data folder

| File | Purpose |
|---|---|
| `jellyfin_names_part01.json` … `part05.json` | The original exports (20 November 2026). Do not change them. |
| `jellyfin_names_part01_edited.json` … `part05_edited.json` | The corrected names. The source of truth for `check-names` and `lock-names`. |
| `test_one_album_edited.json` | The test file for "Brothers in arms". It counts as an edited file. |
| `covers/` | One folder for each album (current and new cover), `index.json` and `choices.json` |
| `.musicbrainz_cache.json` | The MusicBrainz cache (31 MB). It makes later runs fast. |
| `backup-*.json` | Backups of all writes. Example: `backup-locks-20261125-102105.json` for the locks. |

### Possible open items

1. The covers of "Devotional tour - Live in Stockholm '93, Disc 1" and "Promotional" (Depeche mode) can still show the box-set image. MusicBrainz has no different cover. The user can put a cover manually in Jellyfin (⋮ → Edit images).
2. The GitHub token for the push mirror has an expiry date. When it expires, Forgejo shows an error next to the mirror. Then make a new fine-grained token (repository `om1d3/polyhymnia`, Contents: Read and write) and put it in the mirror settings.
3. The `.gitignore` change in the main stack repository is not committed yet.
4. The assistant offered a "Commands" section for the README. The user did not answer yet.

## 5. Procedure for new albums

1. In Jellyfin, start a library scan (Dashboard → Scheduled Tasks → ▶ "Scan Media Library"). Wait until it is complete.
2. Export the new albums:

   ```zsh
   cd /mnt/BIG/filme/docker-compose/polyhymnia/data
   ../jfa export-names --new
   ```

   Result: a list of new albums, and a file `new_albums_<date>-<time>.json`.

3. Copy the file to `/tmp`. Copy it to the laptop with `scp`. Upload it to the assistant.
4. The assistant edits the file with the tool `edit_names.py` (section 9) and the rules in section 6. It examines each change manually. It sends back `new_albums_<date>-<time>_edited.json`. It must not change `id` values. It must keep the format of the file (section 7).
5. Copy the edited file to the data folder. Then do these commands:

   ```zsh
   ../jfa import-names new_albums_<date>-<time>.json new_albums_<date>-<time>_edited.json
   ../jfa import-names new_albums_<date>-<time>.json new_albums_<date>-<time>_edited.json --commit
   ../jfa lock-names new_albums_<date>-<time>_edited.json --commit
   ../jfa bg covers-fetch
   ```

6. When `docker logs --tail 1 jfa-covers-fetch` shows `Next step: jfa covers-review`, do `docker rm jfa-covers-fetch`.
7. Start the picker (`../jfa covers-review`), select the covers, then press Ctrl+C. Start the picker only after the download is complete. The picker reads the album list one time, when it starts.
8. Upload the covers: `../jfa covers-apply`, then `../jfa covers-apply --commit`.
9. Examine all names: `../jfa check-names *_edited.json > final.txt` and `head -n 9 final.txt`.

If the new cover is the same incorrect image as the current cover, use `../jfa covers-fetch --album "name" --refetch --search`.

## 6. Rules for the names (the style of the user)

### Titles and album names

1. Use sentence case: only the first word has a capital. Example: "Wake me up when September ends".
2. Proper nouns keep their capital: names of persons, places, works and organizations. Examples: "Mr. Brightside", "Live in San Diego", "Johnny B. Goode".
3. These words keep their capital: God, Lord, Jesus, Christ, Heaven, Hell, Moon, Sun, Earth, Christmas, the days of the week, the months. "the King", "the Queen", "the Devil" and "the Angel" also keep the capital after "the".
4. The first word after `:`, `.`, `!`, `?` or ` - ` has a capital.
5. Descriptors in brackets are lowercase: "(live)", "(demo)", "(remastered)", "(single version)", "(deluxe edition)". A subtitle or a translation in brackets keeps its first capital: "(Crying)", "(To the other side)".
6. Acronyms keep their capitals: USA, DJ, TV, EP, CD, BBC, AC/DC. Roman numerals are capitals: II, III, IV.
7. Words with an internal capital do not change: McCartney, iPhone, O'Brien.
8. Musical keys keep the capital: "in C#m", "in E-flat major". Catalogue numbers: "op.", "no." in lowercase.
9. German titles: the user also uses sentence case for German. German nouns are lowercase, except proper nouns. Example: "Mein herz brennt (live)".
10. Romanian titles: correct diacritics (ă, â, î, ș, ț). Example: "Rugă la Marea Neagră".
11. Remove the rests of file names: track numbers, "Artist - Album - NN - " prefixes, "-easymp3s", ".mp3".
12. Correct obvious typos. Do not change the words.
13. Do not change official stylizations: MASTER BOOT RECORD, KEYGEN CHURCH titles, Yoko Kanno titles in lowercase, "xx".
14. A title that the user already wrote in sentence case does not change.

### Artist names

1. Persons keep all capitals: "Mark Knopfler", "Leonard Cohen".
2. Bands: words after the first word are lowercase, unless they are names, places, invented spellings or words after an acronym. Examples: "The doors", "Daft punk", "Dire straits", "Red hot chili peppers", "Judas priest", "Ugly kid Joe", "Vaya con Dios".
3. These bands keep the official form of their names: Basement Jaxx, Boys Noize, FC Kahuna, Gogol Bordello, Led Zeppelin, Limp Bizkit, SCEI Sound Team, Space Monkeyz vs. Gorillaz, The Aquatones, The Gigalo's, The Nitecaps, ZZ Top.
4. These names do not change: Pink Floyd, Fleetwood Mac, Gnarls Barkley, Black Sabbath, Ramones.
5. All-capital stylizations keep their capitals: MASTER BOOT RECORD, KEYGEN CHURCH, MONO, UNKLE, CLONA, FREELAND, RIOPY, ATB, INXS.
6. "Alternosfera" and "Laibach" are not in capitals.
7. The user selected these forms before. They stay: "Duran duran", "Fatboy slim", "Guns n' Roses", "Beastie boys", "Emir Kusturica & The no smoking orchestra", "Yoshiki Sugioka from CUBLIC".
8. Orchestras and ensembles follow rule 2: "London philharmonic orchestra", "The Bălănescu quartet".
9. Use one form of the name for each artist in all albums. Merge duplicates, for example "The Ramones" → "Ramones", "Chris Lennertz" → "Christopher Lennertz".
10. Correct broken values: "Laibach - 1985" → "Laibach", "01 Dan Spataru" → "Dan Spătaru", "Cohen, Leonard" → "Leonard Cohen".

## 7. Format of the export files

```json
{
  "format": "jellyfin-music-names",
  "version": 1,
  "server": "http://localhost:8096",
  "exported": "2026-11-20T09:54:20",
  "part": "1 of 1",
  "note": "…",
  "albums": [
    {"album_artist": ["Dire straits"], "album": "Brothers in arms", "id": "cac2723b…", "tracks": [
      {"track": 1, "title": "So far away", "id": "967d2940…"},
      {"disc": 2, "track": 1, "title": "…", "artists": ["…"], "id": "…"}
    ]}
  ]
}
```

1. `album_artist` is a list. `artists` is present only if the track artists are different from the album artists.
2. `disc` is present only for albums with more than one disc.
3. The edited file must keep all `id` values. Change only `album`, `album_artist`, `title` and `artists`.
4. `import-names` changes only the values that are different between the original file and the edited file.

## 8. Lessons from the project

1. Jellyfin shows an artist name only if the name has an artist page. A library scan makes the pages that are not there. Before the scan, `check-names` reports "has no artist page".
2. The online search of Jellyfin can change the name of an artist page. Example: the page of KEYGEN CHURCH got the name "MASTER BOOT RECORD". `check-names --fix` restores the name and locks it.
3. A library scan can change a name that is not locked. All names are now locked.
4. Do not use "Refresh metadata" with "Replace all metadata" in Jellyfin. This action reads all files again.
5. The MusicBrainz IDs in some music files are for a box-set release (Depeche mode). Then `covers-fetch` downloads the box-set image again. `--search` corrects this.
6. After `covers-apply --commit`, a later `covers-apply` shows the uploaded albums as `NOT CHANGED`. This is correct.
7. A file damaged in transfer causes errors. Compare MD5 checksums after each transfer.

## 9. The tool edit_names.py

The folder `tools/editor/` in the repository contains the scripts that edited the names of the library. It has three files:

| File | Contents |
|---|---|
| `edit_names.py` | The program (Python standard library only) |
| `rules.py` | Protected words (715), protected names and phrases (383), band rules (127), hand-made corrections and the 934 artist names of the library |
| `README.md` | The procedure for the tool |

A test with the 5 original exports gave the same 5 edited files, byte for byte. Thus, the tool uses exactly the rules that edited the library.

Procedure for the assistant:

1. Go to the folder `tools/editor/`.
2. Start the tool with the new export file: `python3 edit_names.py new_albums_<date>-<time>.json`.
3. Read `changes_for_review.csv`. Examine each change.
4. If a name, a place or a title of a work became lowercase, add it to `KEEP2` or `PHRASES` in `rules.py`. Then do step 2 again.
5. For a new band, decide the form with the rules in section 6. Add it to `BAND_MAP`.
6. Correct the last special conditions manually, for example Romanian diacritics.
7. Send the edited file to the user. Also send the changed `rules.py`. The user puts it in the repository with a commit.

## 10. Start a new conversation

1. On bender, make the archive of the repository:

   ```zsh
   cd /mnt/BIG/filme/docker-compose/polyhymnia
   git archive --format=zip -o /tmp/polyhymnia-repo.zip HEAD
   chmod 644 /tmp/polyhymnia-repo.zip
   ```

2. On the laptop, copy the archive: `scp admin@10.30.0.12:/tmp/polyhymnia-repo.zip .`
3. In the new conversation, upload `polyhymnia-repo.zip`. The archive contains this document (`docs/HANDOVER.md`) and the tool.
4. Write the first message: "Continue the work from docs/HANDOVER.md in this archive. Use ASD-STE100."
