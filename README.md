<p align="center"><img src="docs/logo.png" width="128" alt="Polyhymnia logo: a golden lyre"></p>

# Polyhymnia

*Named after the Muse of sacred song, keeper of many hymns.*

Polyhymnia checks and fixes the names, artists and covers of a Jellyfin music library, safely:
every change is shown as a dry run first, backed up before it is written, and can be undone.

## Requirements

- A Linux server with Docker and the Docker Compose plugin (`docker compose version` works)
- A Jellyfin API key (Jellyfin: Dashboard → API Keys → +)

## Setup (once per server)

```sh
git clone https://git.home.arpa/horia/polyhymnia.git
cd polyhymnia
chmod +x jfa
cp .env.example .env
nano .env
mkdir -p data
```

In `.env`, fill in `JELLYFIN_URL`, `JELLYFIN_API_KEY` and `MB_CONTACT` (your email address).

## Running it

Always run `jfa` from the folder that holds your data. That folder is where exports,
edited files, backups and downloaded covers are kept:

```sh
cd data
../jfa check-names jellyfin_names_part0*_edited.json
```

Long jobs can run in the background, so they survive logging out:

```sh
../jfa bg covers-fetch
docker logs --tail 3 jfa-covers-fetch
docker rm jfa-covers-fetch
```

The Docker image is built automatically on the first run, and rebuilt after a `git pull`.

## Workflows

**Names** (album names, album artists, track titles, track artists)

| Step | Command |
|---|---|
| 1. Export | `../jfa export-names --split 250` |
| 2. Edit | Edit a copy of each file and name it `…_edited.json` (keep the originals) |
| 3. Dry run | `../jfa import-names jellyfin_names_part01.json jellyfin_names_part01_edited.json` |
| 4. Apply | the same command with `--commit` |
| 5. Check | `../jfa check-names jellyfin_names_part0*_edited.json` |
| 6. Repair | `../jfa check-names jellyfin_names_part0*_edited.json --fix`, then the same with `--commit` |
| 7. Protect | `../jfa lock-names jellyfin_names_part0*_edited.json`, then the same with `--commit` |

**Covers**

| Step | Command |
|---|---|
| 1. Download | `../jfa bg covers-fetch` (current covers are saved at full quality first) |
| 2. Pick | `../jfa covers-review`, then open `http://SERVER-IP:8765` in a browser; Ctrl+C when done |
| 3. Dry run | `../jfa covers-apply` |
| 4. Apply | `../jfa covers-apply --commit` |

The cover picker shows the current and the new cover side by side; every album keeps its
current cover unless you click the new one (demo albums shown):

![Cover picker](docs/cover-picker.png)

**Undo**: every write prints a backup file name. `../jfa restore backup-….json --commit` puts it back.

## Good to know

- Nothing is written without `--commit`.
- `check-names` is read-only unless you add `--fix --commit`. Keep the original exports next to the
  edited files: `check-names` uses them to tell replaced artist names from extra ones in your files.
- A library scan creates missing artist pages. If Jellyfin renames an artist page itself (its online
  lookup can confuse artists), `check-names --fix` renames it back and locks the name.
- On TrueNAS: start `screen` as your normal user and run `sudo -i` inside it. A root shell started
  before `screen` stops being able to run programs once you log out.
