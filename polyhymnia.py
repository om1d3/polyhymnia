#!/usr/bin/env python3
"""
Polyhymnia - check and fix Jellyfin music metadata and covers, safely.
(Named after the Muse of sacred song.)

Names: edit them yourself (or have someone review them) as JSON
  export-names   Write album names, album artists, track titles and track artists to JSON.
  import-names   Compare the original export with your edited copy and change only what
                 you edited. Skips anything that was changed in Jellyfin since the export.

Covers: pick per album in your browser
  covers-fetch   Save every current cover at full quality and download the best front
                 cover from the Cover Art Archive (resumable; run it in screen).
  covers-review  Web page showing current and new cover side by side. Nothing is
                 chosen until you click; choices are saved as you go.
  covers-apply   Upload only the covers you picked.

Report against MusicBrainz
  audit / apply  CSV of differences with MusicBrainz, then push the rows you approve.

Every writing command is a dry run unless --commit is given, and backs up what it
changes first; "restore <backup> --commit" puts it back.

Setup
  pip install requests pillow
  export JELLYFIN_URL=http://192.168.1.10:8096
  export JELLYFIN_API_KEY=xxxxxxxx     (Dashboard > API Keys)
  export MB_CONTACT=you@example.com    (MusicBrainz asks for contact info in the User-Agent)

Examples
  jfa export-names --split 250
  jfa import-names jellyfin_names_part01.json part01_edited.json
  jfa covers-fetch
  jfa covers-review --port 8765
  jfa covers-apply --commit
  jfa audit --compare-covers --sentence-case track
"""
import argparse
import base64
import csv
import difflib
import glob
import html
import io
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode

try:
    import requests
except ImportError:
    sys.exit("This script needs the requests package: pip install requests")

VERSION = "1.1"
CSV_COLUMNS = ["approve", "field", "severity", "confidence", "album_artist", "album",
               "disc", "track", "current", "proposed", "similarity", "note",
               "album_id", "item_id", "mb_release_id"]
FIXABLE = {"album_name", "album_artist", "track_name", "cover"}
APPROVE_YES = {"y", "yes", "x", "1", "true"}
ARTIST_SEP = "; "
GENERIC_TITLE = re.compile(r"^\s*(track|piste|titel)?\s*[-_.]?\s*\d+\s*$", re.I)

# --------------------------------------------------------------------------- text helpers

_PUNCT = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2026": "...",
})


def straighten(s):
    """Typographic quotes/dashes (MusicBrainz style) -> plain ASCII."""
    return (s or "").translate(_PUNCT)


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").translate(_PUNCT)
    return re.sub(r"\s+", " ", s).strip().casefold()


def loose(s):
    return re.sub(r"[\W_]+", "", norm(s))


def sim(a, b):
    a, b = loose(a), loose(b)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def classify(current, proposed):
    """None if identical, otherwise 'missing', 'cosmetic' (case/punctuation) or 'mismatch'."""
    current, proposed = current or "", proposed or ""
    if current == proposed:
        return None
    if not current.strip():
        return "missing"
    if norm(current) == norm(proposed):
        return "cosmetic"
    return "mismatch"


def credit_names(ac):
    return [c.get("name") or (c.get("artist") or {}).get("name", "") for c in ac or []]


def credit_string(ac):
    return "".join((c.get("name") or (c.get("artist") or {}).get("name", "")) + c.get("joinphrase", "")
                   for c in ac or [])

# --------------------------------------------------------------------------- sentence case

NO_BREAK_ABBR = {"feat", "ft", "vs", "vol", "no", "nr", "pt", "op", "ca", "aka"}
NAMES_FOLLOW = {"feat", "ft", "featuring", "vs"}   # what comes after these is artist names
SHOUT_ROMAN = re.compile(r"^(?=[IVX]{2,5}$)(X{0,3})(IX|IV|V?I{0,3})$")


def load_keep(path, explicit):
    """Words/phrases from keep_caps.txt that always keep the spelling written in the file."""
    if not os.path.exists(path):
        if explicit:
            sys.exit(f"Keep-capitals file {path!r} not found.")
        return []
    with open(path, encoding="utf-8-sig") as f:
        words = {ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")}
    return [(re.compile(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", re.I), w)
            for w in sorted(words, key=len, reverse=True)]


def _case_core(core, start, shouting, trail):
    if re.fullmatch(r"I(['’]\w+)?", core):  # English "I", "I'm" - never touch an existing capital I
        return core
    if shouting:
        if SHOUT_ROMAN.match(core):
            return core
    else:
        if len(core) > 1 and core.isupper():                      # USA, DJ, AC/DC, II
            return core
        if any(a.islower() and b.isupper() for a, b in zip(core, core[1:])):  # McCartney, iPhone
            return core
        if re.match(r"[A-Z]['’][A-Z]", core):                     # O'Brien, D'Angelo
            return core
        if len(core) == 1 and core.isupper() and trail.startswith("."):  # initials: Johnny B. Goode
            return core
    low = core.lower()
    return low[:1].upper() + low[1:] if start else low


def sentence_case(text, keep=()):
    """'Wake Me Up When September Ends' -> 'Wake me up when September ends' (with September kept)."""
    if not text:
        return text or ""
    letters = [c for c in text if c.isalpha()]
    words = [t for t in text.split() if any(c.isalpha() for c in t)]
    shouting = len(words) >= 2 and len(letters) >= 4 and all(c.isupper() for c in letters)
    out, start, names = [], True, False
    for tok in re.split(r"(\s+)", text):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        lead, core, trail = re.match(r"^(\W*)(.*?)(\W*)$", tok, re.S).groups()
        if not core:
            out.append(tok)
            if re.fullmatch(r"[-–—/|]+", tok):
                start = True
            continue
        out.append(lead + (core if names and not shouting else _case_core(core, start, shouting, trail)) + trail)
        key = core.casefold()
        if key in NAMES_FOLLOW:
            names = True
        elif names and re.search(r"[)\]]", trail):
            names = False
        start = bool(re.search(r"[.!?:]", trail)) and key not in NO_BREAK_ABBR
    result = "".join(out)
    for pattern, word in keep:
        result = pattern.sub(word, result)
    return result

# --------------------------------------------------------------------------- Jellyfin


class Jellyfin:
    def __init__(self, url, api_key, user=None):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = (
            f'MediaBrowser Token="{api_key}", Client="polyhymnia", '
            f'Device="script", DeviceId="polyhymnia", Version="{VERSION}"')
        self.user_id = self._resolve_user(user)

    def _req(self, method, path, **kw):
        r = self.s.request(method, self.url + path, timeout=120, **kw)
        if r.status_code == 401:
            sys.exit("Jellyfin returned 401 Unauthorized - check JELLYFIN_API_KEY.")
        if not r.ok:
            raise RuntimeError(f"Jellyfin {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    def get(self, path, **params):
        return self._req("GET", path, params=params).json()

    def _resolve_user(self, name):
        users = self.get("/Users")
        if not users:
            sys.exit("Jellyfin returned no users.")
        if name:
            for u in users:
                if u["Name"].casefold() == name.casefold():
                    return u["Id"]
            sys.exit(f"No Jellyfin user named {name!r}.")
        for u in users:
            if (u.get("Policy") or {}).get("IsAdministrator"):
                return u["Id"]
        return users[0]["Id"]

    def music_library_id(self, name):
        views = self.get("/UserViews", userId=self.user_id).get("Items", [])
        music = [v for v in views if v.get("CollectionType") == "music"]
        for v in music:
            if v["Name"].casefold() == name.casefold():
                return v["Id"]
        sys.exit(f"No music library named {name!r}. Found: {', '.join(v['Name'] for v in music) or 'none'}")

    def _items(self, **params):
        start = 0
        while True:
            data = self.get("/Items", userId=self.user_id, StartIndex=start, Limit=200, **params)
            batch = data.get("Items", [])
            yield from batch
            start += len(batch)
            if not batch or start >= data.get("TotalRecordCount", 0):
                break

    def albums(self, library_id=None):
        params = dict(IncludeItemTypes="MusicAlbum", Recursive="true",
                      Fields="ProviderIds,Path", SortBy="SortName")
        if library_id:
            params["ParentId"] = library_id
        albums = list(self._items(**params))
        return sorted(albums, key=lambda a: ((a.get("AlbumArtist") or "").casefold(),
                                             (a.get("Name") or "").casefold()))

    def tracks(self, album_id):
        tracks = list(self._items(ParentId=album_id, IncludeItemTypes="Audio", Recursive="true",
                                  Fields="ProviderIds,Path", SortBy="SortName"))
        return sorted(tracks, key=lambda t: (t.get("ParentIndexNumber") or 1,
                                             t.get("IndexNumber") or 10_000,
                                             (t.get("Name") or "").casefold()))

    def item(self, item_id):
        return self.get(f"/Items/{item_id}", userId=self.user_id)

    def artist_pages(self):
        """id -> name of every artist page Jellyfin has (performers and album artists)."""
        pages = {}
        for path in ("/Artists", "/Artists/AlbumArtists"):
            start = 0
            while True:
                data = self.get(path, userId=self.user_id, StartIndex=start, Limit=500)
                items = data.get("Items", [])
                for it in items:
                    if it.get("Id") and it.get("Name") is not None:
                        pages.setdefault(it["Id"], it["Name"])
                start += len(items)
                if not items or start >= data.get("TotalRecordCount", 0):
                    break
        return pages

    def artist(self, name):
        """The artist entry Jellyfin keeps for this name, or None."""
        try:
            r = self.s.get(f"{self.url}/Artists/{quote(name, safe='')}", params={"userId": self.user_id}, timeout=60)
        except requests.RequestException:
            return None
        return r.json() if r.ok else None

    def update_item(self, dto):
        # Jellyfin wants the complete item back, so always GET -> modify -> POST.
        self._req("POST", f"/Items/{dto['Id']}", json=dto)

    def image(self, item_id, max_width=None):
        params = {"maxWidth": max_width} if max_width else {}
        r = self.s.get(f"{self.url}/Items/{item_id}/Images/Primary", params=params, timeout=120,
                       headers={"Accept": "image/jpeg, image/png;q=0.9"})
        if r.status_code == 404:
            return None, None
        if not r.ok:
            raise RuntimeError(f"Could not read cover of {item_id}: HTTP {r.status_code}")
        return r.content, r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()

    def set_image(self, item_id, data, content_type):
        if content_type in ("image/jpg", "image/pjpeg"):
            content_type = "image/jpeg"  # Jellyfin rejects image/jpg
        self._req("POST", f"/Items/{item_id}/Images/Primary",
                  data=base64.b64encode(data), headers={"Content-Type": content_type})

    def delete_image(self, item_id):
        self._req("DELETE", f"/Items/{item_id}/Images/Primary")

# --------------------------------------------------------------------------- MusicBrainz


def _esc(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


class MusicBrainz:
    API = "https://musicbrainz.org/ws/2"

    def __init__(self, contact, cache_path):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"polyhymnia/{VERSION} ( {contact} )"
        self.s.headers["Accept"] = "application/json"
        self.cache_path, self.cache, self._last, self._dirty = cache_path, {}, 0.0, 0
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                self.cache = json.load(f)

    def get(self, path, **params):
        params["fmt"] = "json"
        key = path + "?" + urlencode(sorted(params.items()))
        if key in self.cache:
            return self.cache[key]
        data = None
        for attempt in range(6):
            wait = 1.1 - (time.monotonic() - self._last)  # MusicBrainz allows ~1 request/second
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.s.get(self.API + path, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (400, 404):
                break
            r.raise_for_status()
            data = r.json()
            break
        else:
            raise RuntimeError(f"MusicBrainz kept refusing {path}")
        self.cache[key] = data
        self._dirty += 1
        if self._dirty >= 20:
            self.save()
        return data

    def save(self):
        if self.cache_path and self._dirty:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
            os.replace(tmp, self.cache_path)
        self._dirty = 0

    def release(self, mbid):
        return self.get(f"/release/{mbid}", inc="recordings artist-credits release-groups")

    def search_releases(self, title, artist):
        q = f'release:"{_esc(title)}"' + (f' AND artist:"{_esc(artist)}"' if artist else "")
        return (self.get("/release", query=q, limit="15") or {}).get("releases", [])

    def search_releases_by_track(self, track_title, artist):
        q = f'recording:"{_esc(track_title)}"' + (f' AND artist:"{_esc(artist)}"' if artist else "")
        out = []
        for rec in (self.get("/recording", query=q, limit="25") or {}).get("recordings", []):
            for rel in rec.get("releases", []):
                rel = dict(rel)
                rel.setdefault("artist-credit", rec.get("artist-credit", []))
                out.append(rel)
        return out


def _candidate_track_count(rel):
    return rel.get("track-count") or sum(m.get("track-count", 0) for m in rel.get("media", []))


def pick_release(mb, title, artist, track_count, track_titles, rg_id=None):
    """Return the best-scoring MusicBrainz release id for an album without a stored MBID."""
    seen, scored = set(), []

    def consider(cands):
        for c in cands:
            if c.get("id") in seen:
                continue
            seen.add(c.get("id"))
            s = (0.45 * sim(title, c.get("title"))
                 + 0.35 * sim(artist, credit_string(c.get("artist-credit")))
                 + (0.2 if _candidate_track_count(c) == track_count else 0))
            if rg_id and (c.get("release-group") or {}).get("id") == rg_id:
                s += 0.1
            if c.get("status") == "Official":
                s += 0.01
            scored.append((s, c["id"]))

    consider(mb.search_releases(title, artist))
    if max(scored, default=(0, None))[0] < 0.9:
        # Album title may be the thing that's wrong: look for releases containing its tracks.
        for tt in [t for t in track_titles if t and not GENERIC_TITLE.match(t)][:2]:
            consider(mb.search_releases_by_track(tt, artist))
    best = max(scored, default=(0, None))
    return best[1] if best[0] >= 0.4 else None


def match_confidence(title, artist, track_count, release):
    t = sim(title, release.get("title"))
    a = sim(artist, credit_string(release.get("artist-credit")))
    same_count = sum(len(m.get("tracks", [])) for m in release.get("media", [])) == track_count
    if t >= 0.9 and a >= 0.85 and same_count:
        return "high"
    if same_count and t >= 0.6 and a >= 0.6:
        return "medium"
    return "low"

def album_artist_names(album):
    names = [a["Name"] for a in album.get("AlbumArtists") or [] if a.get("Name")]
    return names or ([album["AlbumArtist"]] if album.get("AlbumArtist") else [])


def find_release(mb, album, tracks):
    """MusicBrainz release for a Jellyfin album: stored MBID first, then a search. -> (release, confidence)"""
    name, artists = album.get("Name") or "", album_artist_names(album)
    pids = album.get("ProviderIds") or {}
    if pids.get("MusicBrainzAlbum"):
        release = mb.release(pids["MusicBrainzAlbum"])
        if release:
            return release, "mbid"
    rid = pick_release(mb, name, artists[0] if artists else "", len(tracks),
                       [t.get("Name") or "" for t in tracks], pids.get("MusicBrainzReleaseGroup"))
    release = mb.release(rid) if rid else None
    if release:
        return release, match_confidence(name, ARTIST_SEP.join(artists), len(tracks), release)
    return None, None


def select_albums(jf, args):
    """Albums filtered by --library / --album / --limit; also returns the library id."""
    lib = jf.music_library_id(args.library) if getattr(args, "library", None) else None
    albums = jf.albums(lib)
    if getattr(args, "album", None):
        needle = args.album.casefold()
        albums = [a for a in albums
                  if needle in f"{a.get('Name', '')} {a.get('AlbumArtist') or ''}".casefold()]
    if getattr(args, "limit", None):
        albums = albums[:args.limit]
    return albums, lib


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def eta_text(started, done, total):
    if done < 5:
        return ""
    left = (time.monotonic() - started) / done * (total - done)
    return f"  (about {left / 60:.0f} min left)"

# --------------------------------------------------------------------------- covers


def cover_url(http, release):
    if (release.get("cover-art-archive") or {}).get("front"):
        return f"https://coverartarchive.org/release/{release['id']}/front-1200"
    rg = (release.get("release-group") or {}).get("id")
    if rg:
        url = f"https://coverartarchive.org/release-group/{rg}/front-1200"
        try:
            if http.head(url, allow_redirects=False, timeout=30).status_code in (200, 301, 302, 307, 308):
                return url
        except requests.RequestException:
            pass
    return None


def fetch_cover(http, src):
    if os.path.isfile(src):
        ext = os.path.splitext(src)[1].lower()
        ctype = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
        with open(src, "rb") as f:
            return f.read(), ctype
    urls = [src]
    m = re.match(r"(.*/front)-\d+$", src)
    if m:
        urls += [m.group(1) + "-500", m.group(1)]
    for u in urls:
        r = http.get(u, timeout=120)
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
        if r.ok and ctype.startswith("image/"):
            return r.content, ctype
    raise RuntimeError(f"Could not download a cover image from {src}")


def dhash(img_bytes, size=8):
    from PIL import Image
    resample = getattr(Image, "Resampling", Image).LANCZOS
    im = Image.open(io.BytesIO(img_bytes)).convert("L").resize((size + 1, size), resample)
    px, bits = list(im.getdata()), 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return bits

# --------------------------------------------------------------------------- audit


def audit_album(jf, mb, http, album, args, rows, cover_review):
    name = album.get("Name") or ""
    jf_artists = [a["Name"] for a in album.get("AlbumArtists") or [] if a.get("Name")]
    if not jf_artists and album.get("AlbumArtist"):
        jf_artists = [album["AlbumArtist"]]
    artist_str = ARTIST_SEP.join(jf_artists)
    tracks = jf.tracks(album["Id"])
    base = {"album_artist": artist_str, "album": name, "album_id": album["Id"]}
    added = []
    artist_note = ("Sentence case on an artist name - names like 'Pink Floyd' keep their capitals, "
                   "check before approving")

    def add(**kw):
        row = dict.fromkeys(CSV_COLUMNS, "")
        row.update(base)
        row.update(kw)
        rows.append(row)
        added.append(row)

    def tidy(kind, text):
        """Apply --straight-quotes and --sentence-case to a value."""
        text = straighten(text) if args.straight_quotes else (text or "")
        return sentence_case(text, args.keep) if kind in args.sentence_case else text

    def case_only(field, current, proposed, **kw):
        """Row for a capitalization/punctuation fix of Jellyfin's own value (no MusicBrainz data)."""
        sev = classify(current, proposed)
        if sev:
            add(approve="" if field == "album_artist" else "y", field=field, severity=sev,
                confidence=kw.pop("confidence", "none"), current=current, proposed=proposed,
                similarity=f"{sim(current, proposed):.2f}",
                note=artist_note if field == "album_artist" else "Capitalization/punctuation only", **kw)

    def case_only_track(tr, **kw):
        cur = tr.get("Name") or ""
        case_only("track_name", cur, tidy("track", cur), item_id=tr["Id"],
                  disc=tr.get("ParentIndexNumber") or "", track=tr.get("IndexNumber") or "", **kw)

    if not tracks:
        add(field="info", severity="empty", note="Album has no tracks in Jellyfin")
        return "review"

    # 1. find the MusicBrainz release
    release, conf = find_release(mb, album, tracks)
    if not release:
        add(field="info", severity="no-match",
            note="No MusicBrainz match - check the name/artist by hand or tag the files with Picard")
        # still offer capitalization fixes of what Jellyfin already has
        case_only("album_name", name, tidy("album", name))
        case_only("album_artist", artist_str, ARTIST_SEP.join(tidy("artist", a) for a in jf_artists))
        for tr in tracks:
            case_only_track(tr)
        return "no-match"

    base["mb_release_id"] = release["id"]
    approve = "y" if conf in ("mbid", "high") else ""

    # 2. album name
    proposed = tidy("album", release.get("title"))
    sev = classify(name, proposed)
    if sev:
        add(approve=approve, field="album_name", severity=sev, confidence=conf, current=name,
            proposed=proposed, similarity=f"{sim(name, proposed):.2f}",
            note="Also updates the Album field of every track")

    # 3. album artist
    ac = release.get("artist-credit", [])
    mb_names = [tidy("artist", n) for n in credit_names(ac)]
    mb_full = "".join(n + (c.get("joinphrase") or "") for n, c in zip(mb_names, ac))
    if jf_artists not in (mb_names, [mb_full]):
        proposed = mb_full if len(jf_artists) <= 1 else ARTIST_SEP.join(mb_names)
        sev = classify(artist_str, proposed)
        if sev:
            cased = "artist" in args.sentence_case
            add(approve="" if cased else approve, field="album_artist", severity=sev, confidence=conf,
                current=artist_str, proposed=proposed, similarity=f"{sim(artist_str, proposed):.2f}",
                note=(artist_note + "; " if cased else "") + "Applied to the album and all of its tracks")

    # 4. cover
    has_cover = bool((album.get("ImageTags") or {}).get("Primary"))
    caa = cover_url(http, release)
    thumb = re.sub(r"-1200$", "-250", caa) if caa else None
    if not has_cover:
        add(approve=approve if caa else "", field="cover", severity="missing", confidence=conf,
            current="(none)", proposed=caa or "",
            note="" if caa else "No cover on Cover Art Archive either - put a URL or file path in proposed")
        if caa:
            cover_review.append({"kind": "missing", "album": name, "artist": artist_str, "jf": None,
                                 "mb": thumb, "mb_full": caa, "note": f"confidence: {conf}"})
    elif args.compare_covers and caa:
        jf_img, jf_type = jf.image(album["Id"], max_width=300)
        ref = http.get(thumb, timeout=60)
        if jf_img and ref.ok:
            try:
                d = bin(dhash(jf_img) ^ dhash(ref.content)).count("1")
            except Exception as e:  # unreadable image
                add(field="cover", severity="unreadable", current="(existing cover)", proposed=caa,
                    confidence=conf, note=f"Could not compare covers: {e}")
            else:
                if d > args.cover_threshold:
                    add(field="cover", severity="differs", confidence=conf, current="(existing cover)",
                        proposed=caa, similarity=f"{1 - d / 64:.2f}",
                        note=f"Looks different from MusicBrainz art (distance {d}/64); may be another "
                             "edition - see the _covers.html page before approving")
                    cover_review.append({
                        "kind": "differs", "album": name, "artist": artist_str, "mb": thumb, "mb_full": caa,
                        "jf": f"data:{jf_type or 'image/jpeg'};base64,{base64.b64encode(jf_img).decode()}",
                        "note": f"difference {d}/64, confidence: {conf}"})

    # 5. tracks
    media = release.get("media", [])
    multi_disc = len(media) > 1
    mb_tracks = {}
    for m in media:
        for t in m.get("tracks", []):
            mb_tracks[(m.get("position", 1) if multi_disc else 1, t.get("position"))] = tidy("track", t.get("title"))
    used, unmatched = set(), []
    for tr in tracks:
        cur, note = tr.get("Name") or "", ""
        key = ((tr.get("ParentIndexNumber") or 1) if multi_disc else 1, tr.get("IndexNumber"))
        if key[1] is None or key not in mb_tracks or key in used:
            best = max(((sim(cur, title), k) for k, title in mb_tracks.items() if k not in used),
                       default=(0, None))
            if best[0] < 0.75:
                unmatched.append(tr)
                continue
            key, note = best[1], "Matched by title (track number missing or different)"
        used.add(key)
        proposed = mb_tracks[key]
        sev = classify(cur, proposed)
        if not sev:
            continue
        s = sim(cur, proposed)
        ok = s >= 0.5 or GENERIC_TITLE.match(cur) or sev == "missing"
        if not ok:
            note = (note + "; " if note else "") + "Very different title - check the track order"
        add(approve=approve if ok else "", field="track_name", severity=sev, confidence=conf,
            disc=key[0] if multi_disc else "", track=key[1], current=cur, proposed=proposed,
            similarity=f"{s:.2f}", item_id=tr["Id"], note=note)

    mb_count = len(mb_tracks)
    if len(tracks) != mb_count:
        add(field="info", severity="track-count", confidence=conf,
            note=f"Jellyfin has {len(tracks)} tracks, the MusicBrainz release has {mb_count}")
    for tr in unmatched:
        add(field="info", severity="extra-track", confidence=conf, item_id=tr["Id"],
            disc=tr.get("ParentIndexNumber") or "", track=tr.get("IndexNumber") or "",
            current=tr.get("Name") or "", note="No matching track on the MusicBrainz release")
        case_only_track(tr, confidence=conf)
    missing = [mb_tracks[k] for k in mb_tracks if k not in used]
    if missing:
        shown = "; ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
        add(field="info", severity="missing-tracks", confidence=conf,
            note=f"{len(missing)} MusicBrainz track(s) not in Jellyfin: {shown}")

    return "review" if added else "clean"


def write_cover_review(path, items):
    esc = html.escape

    def table(kind, title, explain):
        sel = [i for i in items if i["kind"] == kind]
        if not sel:
            return ""
        cells = []
        for i in sel:
            jf_cell = f'<img src="{i["jf"]}" alt="">' if i["jf"] else "<i>no cover</i>"
            cells.append(
                f'<tr><td><b>{esc(i["album"])}</b><br>{esc(i["artist"])}<br><small>{esc(i["note"])}</small></td>'
                f'<td>{jf_cell}</td><td><a href="{esc(i["mb_full"])}">'
                f'<img src="{esc(i["mb"])}" loading="lazy" alt=""></a></td></tr>')
        body = "".join(cells)
        return (f"<h2>{title} ({len(sel)})</h2><p>{explain}</p><table><tr><th>Album</th>"
                f"<th>In Jellyfin now</th><th>MusicBrainz suggestion</th></tr>{body}</table>")

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Cover review</title><style>
:root {{ --bg:#fff; --fg:#1d1d1f; --line:#ddd; --muted:#666; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1b1b1d; --fg:#eee; --line:#3a3a3d; --muted:#aaa; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.4 system-ui,sans-serif; margin:2rem; }}
table {{ border-collapse:collapse; }} td,th {{ border-bottom:1px solid var(--line); padding:.6rem 1rem; text-align:left; vertical-align:top; }}
img {{ width:200px; height:200px; object-fit:contain; }} small,p {{ color:var(--muted); }}
</style></head><body><h1>Cover review</h1>
<p>Generated {datetime.now():%Y-%m-%d %H:%M}. To use a MusicBrainz cover, put <b>y</b> in the approve column of that
album's <b>cover</b> row in the CSV. Rows you leave empty are never changed.</p>
{table("differs", "Existing covers that look different", "Leave these alone if your current cover is the right one.")}
{table("missing", "Albums without a cover", "These are pre-approved in the CSV when the album match is confident.")}
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def cmd_audit(args):
    if args.compare_covers:
        try:
            import PIL  # noqa: F401
        except ImportError:
            sys.exit("--compare-covers needs Pillow: pip install pillow")
    fields = {f.strip().lower() for f in (args.sentence_case or "").split(",") if f.strip()}
    if "all" in fields:
        fields = {"track", "album", "artist"}
    if fields - {"track", "album", "artist"}:
        sys.exit("--sentence-case takes a comma list of: track, album, artist (or all)")
    args.sentence_case = fields
    args.keep = load_keep(args.keep_caps or "keep_caps.txt", explicit=bool(args.keep_caps)) if fields else []
    if fields:
        print(f"Sentence case for: {', '.join(sorted(fields))}; {len(args.keep)} protected words/phrases",
              file=sys.stderr)
    if args.contact == "no-contact-given":
        print("Tip: set MB_CONTACT (an email or URL) - MusicBrainz may throttle anonymous scripts.",
              file=sys.stderr)
    jf = make_jf(args)
    mb = MusicBrainz(args.contact, args.cache)
    http = requests.Session()
    http.headers["User-Agent"] = mb.s.headers["User-Agent"]

    albums, _ = select_albums(jf, args)

    rows, stats, cover_review = [], Counter(), []
    started = time.monotonic()
    try:
        for i, album in enumerate(albums, 1):
            eta = ""
            if i > 5:
                left = (time.monotonic() - started) / (i - 1) * (len(albums) - i + 1)
                eta = f"  (about {left / 60:.0f} min left)"
            print(f"[{i}/{len(albums)}] {album.get('AlbumArtist') or '?'} - {album.get('Name')}{eta}",
                  file=sys.stderr, flush=True)
            try:
                stats[audit_album(jf, mb, http, album, args, rows, cover_review)] += 1
            except Exception as e:
                stats["error"] += 1
                row = dict.fromkeys(CSV_COLUMNS, "")
                row.update(field="info", severity="error", album=album.get("Name") or "",
                           album_artist=album.get("AlbumArtist") or "", album_id=album["Id"],
                           note=f"{type(e).__name__}: {e}")
                rows.append(row)
    except KeyboardInterrupt:
        print("\nInterrupted - writing a partial report.", file=sys.stderr)
    finally:
        mb.save()

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    fixes = [r for r in rows if r["field"] in FIXABLE and r["proposed"]]
    print(f"\n{sum(stats.values())} albums checked: {stats['clean']} clean, {stats['review']} with "
          f"differences, {stats['no-match']} not found on MusicBrainz, {stats['error']} errors.")
    print(f"{len(fixes)} proposed fixes ({sum(r['approve'] == 'y' for r in fixes)} pre-approved) "
          f"written to {args.output}")
    if cover_review:
        review_path = os.path.splitext(args.output)[0] + "_covers.html"
        write_cover_review(review_path, cover_review)
        print(f"Side-by-side cover comparison: {review_path}")
    print(f"Review it, then: jfa apply {args.output}")


# --------------------------------------------------------------------------- writing to Jellyfin

MAX_UPLOAD = 15 * 1024 * 1024
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
EXT_TYPE = {v: k for k, v in IMAGE_EXT.items()} | {".jpeg": "image/jpeg"}


def _field_value(dto, field):
    if field == "AlbumArtists":
        names = [a.get("Name") for a in dto.get("AlbumArtists") or [] if a.get("Name")]
        return names or ([dto["AlbumArtist"]] if dto.get("AlbumArtist") else [])
    if field == "Artists":
        return [a.get("Name") for a in dto.get("ArtistItems") or [] if a.get("Name")] or list(dto.get("Artists") or [])
    return dto.get(field) or ""


def _same(field, current, exported):
    """Artist lists count as unchanged when Jellyfin reports the exported names with other spacing, order
    or capitalization, or leaves some out: it reports artists through their artist pages, and drops names
    it can't link to one (e.g. after a page was renamed). A name that wasn't in the export is a real edit."""
    if field in ("AlbumArtists", "Artists"):
        return {x.strip().casefold() for x in current or []} <= {x.strip().casefold() for x in exported or []}
    return current == exported


def _name_pairs(names, existing):
    ids = {(a.get("Name") or "").casefold(): a.get("Id") for a in existing or []}
    return [{"Name": n, **({"Id": ids[n.casefold()]} if ids.get(n.casefold()) else {})} for n in names]


def shrink_if_huge(data, ctype):
    if len(data) <= MAX_UPLOAD:
        return data, ctype, ""
    from PIL import Image
    out = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(out, "JPEG", quality=95)
    return out.getvalue(), "image/jpeg", f" (re-encoded as JPEG, the {len(data) / 1048576:.0f} MB original is too big to upload)"


def run_changes(jf, changes, args, expected=None, covers=None, follow=None, stored_artists=False):
    """Show, and with --commit apply, metadata changes and cover uploads. Backs everything up first.

    changes:  item_id -> any of {"Name", "Album", "AlbumArtists", "Artists"}
    expected: item_id -> {field: value Jellyfin must still have}; a field that no longer matches is skipped
    covers:   album_id -> {"src": URL or file, "label": str, optional "expect_tag", optional "backup_file"}
    follow:   like expected, but for values that only follow another edit: dropped quietly if they differ
    """
    expected, covers, follow = expected or {}, covers or {}, follow or {}
    planned, skipped = [], 0
    for item_id, ch in changes.items():
        try:
            dto = jf.item(item_id)
        except RuntimeError as e:
            print(f"SKIP item {item_id}: not found in Jellyfin ({e})")
            skipped += 1
            continue
        label = f"{dto.get('Type', 'Item')} {dto.get('Name')!r}"
        ch = dict(ch)
        for field, want in expected.get(item_id, {}).items():
            if field in ch and not _same(field, _field_value(dto, field), want):
                print(f"SKIP {label}: {field} is {_field_value(dto, field)!r} in Jellyfin but was {want!r} "
                      "in the export, so it was changed since - leaving it alone")
                ch.pop(field)
                skipped += 1
        for field, want in follow.get(item_id, {}).items():
            if field in ch and not _same(field, _field_value(dto, field), want):
                ch.pop(field)
        def now(f):   # check-names compares track artists with the stored list, which includes hidden names
            if stored_artists and f == "Artists" and dto.get("Type") == "Audio" and "Artists" in dto:
                return list(dto.get("Artists") or [])
            return _field_value(dto, f)
        diffs = [f"{f}: {now(f)!r} -> {v!r}" for f, v in ch.items() if now(f) != v]
        if diffs:
            planned.append((dto, ch))
            print(f"{label}: " + " | ".join(diffs))

    planned_covers = []
    for aid, spec in covers.items():
        if "expect_tag" in spec:
            try:
                tag = (jf.item(aid).get("ImageTags") or {}).get("Primary")
            except RuntimeError:
                print(f"SKIP cover for {spec.get('label', aid)}: album not found in Jellyfin")
                continue
            if tag != spec["expect_tag"]:
                print(f"SKIP cover for {spec.get('label', aid)}: the cover was changed in Jellyfin after "
                      "covers-fetch, leaving it alone")
                skipped += 1
                continue
        planned_covers.append((aid, spec))
        print(f"Cover for {spec.get('label', aid)} <- {spec['src']}")

    if not planned and not planned_covers:
        print("Nothing to change." + (f" ({skipped} change(s) skipped, see above)" if skipped else ""))
        return
    print(f"\n{len(planned)} item(s) and {len(planned_covers)} cover(s) to update"
          + (f", {skipped} change(s) skipped." if skipped else "."))
    if not args.commit:
        print("Dry run only - nothing was changed. Re-run with --commit to apply.")
        return

    backup_path = args.backup or f"backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    backup = {"created": datetime.now().isoformat(), "server": jf.url,
              "items": [dto for dto, _ in planned], "images": []}
    for aid, spec in planned_covers:
        if "backup_file" in spec:   # covers-fetch already saved the current cover at full quality
            f = spec["backup_file"]
            backup["images"].append({"id": aid, "had_image": bool(f), "file": os.path.abspath(f) if f else None,
                                     "content_type": EXT_TYPE.get(os.path.splitext(f or "")[1].lower(), "image/jpeg")})
        else:
            data, ctype = jf.image(aid)
            backup["images"].append({"id": aid, "had_image": data is not None, "content_type": ctype,
                                     "data": base64.b64encode(data).decode() if data else None})
    save_json(backup_path, backup)
    print(f"Backup of the original values written to {backup_path}")

    ok = failed = 0
    for dto, ch in planned:
        new = json.loads(json.dumps(dto))
        if "Name" in ch:
            new["Name"] = ch["Name"]
            if not getattr(args, "no_lock", False):
                new["LockedFields"] = sorted(set(new.get("LockedFields") or []) | {"Name"})
        if "Album" in ch:
            new["Album"] = ch["Album"]
        if "AlbumArtists" in ch:
            new["AlbumArtists"] = _name_pairs(ch["AlbumArtists"], dto.get("AlbumArtists"))
        if "Artists" in ch:
            new["ArtistItems"] = _name_pairs(ch["Artists"], dto.get("ArtistItems"))
            new["Artists"] = list(ch["Artists"])
        elif dto.get("Artists") and dto.get("Type") == "Audio":
            # ArtistItems can leave out names Jellyfin can't link to an artist page; the plain
            # Artists list is what is stored, so write that back to avoid dropping anyone
            new["ArtistItems"] = _name_pairs(list(dto["Artists"]), dto.get("ArtistItems"))
        if getattr(args, "lock_item", False):
            new["LockData"] = True
        try:
            jf.update_item(new)
            ok += 1
        except Exception as e:
            failed += 1
            print(f"FAILED {dto.get('Name')!r}: {e}")

    http = requests.Session()
    http.headers["User-Agent"] = f"polyhymnia/{VERSION} ( {args.contact} )"
    for aid, spec in planned_covers:
        try:
            data, ctype = fetch_cover(http, spec["src"])
            data, ctype, note = shrink_if_huge(data, ctype)
            jf.set_image(aid, data, ctype)
            ok += 1
            if note:
                print(f"Cover for {spec.get('label', aid)}{note}")
        except Exception as e:
            failed += 1
            print(f"FAILED cover for {spec.get('label', aid)}: {e}")
    print(f"Done: {ok} updated, {failed} failed. Undo with: "
          f"jfa restore {backup_path} --commit")


def cmd_apply(args):
    jf = make_jf(args)
    with open(args.report, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f)
                if (r.get("approve") or "").strip().lower() in APPROVE_YES and r.get("field") in FIXABLE]
    if not rows:
        print("No approved rows in the report (put y in the approve column).")
        return
    changes, covers, track_ids = defaultdict(dict), {}, {}

    def album_tracks(aid):
        if aid not in track_ids:
            track_ids[aid] = [t["Id"] for t in jf.tracks(aid)]
        return track_ids[aid]

    for r in rows:
        field, aid, prop = r["field"], r["album_id"], r["proposed"].strip()
        if not prop:
            print(f"Skipping {field} for {r['album']!r}: proposed value is empty")
        elif field == "track_name":
            changes[r["item_id"]]["Name"] = prop
        elif field == "album_name":
            changes[aid]["Name"] = prop
            for tid in album_tracks(aid):
                changes[tid]["Album"] = prop
        elif field == "album_artist":
            names = [n.strip() for n in prop.split(";") if n.strip()]
            for iid in [aid] + album_tracks(aid):
                changes[iid]["AlbumArtists"] = names
        elif field == "cover":
            covers[aid] = {"src": prop, "label": f"{r['album_artist']} - {r['album']}"}
    run_changes(jf, changes, args, covers=covers)


def cmd_restore(args):
    jf = make_jf(args)
    with open(args.backup, encoding="utf-8") as f:
        backup = json.load(f)
    print(f"Backup from {backup['created']}: {len(backup['items'])} item(s), {len(backup['images'])} cover(s).")
    if not args.commit:
        print("Dry run only - re-run with --commit to restore.")
        return
    for dto in backup["items"]:
        if dto.get("Type") == "Audio" and dto.get("Artists"):   # stored list, see run_changes
            dto = dict(dto, ArtistItems=_name_pairs(list(dto["Artists"]), dto.get("ArtistItems")))
        jf.update_item(dto)
    for img in backup["images"]:
        if not img["had_image"]:
            jf.delete_image(img["id"])
        elif img.get("data"):
            jf.set_image(img["id"], base64.b64decode(img["data"]), img["content_type"])
        else:
            with open(img["file"], "rb") as f:
                jf.set_image(img["id"], f.read(), img["content_type"])
    print("Restored.")

# --------------------------------------------------------------------------- names export / import

NAMES_FORMAT = "jellyfin-music-names"
NAMES_NOTE = ("Edit album, album_artist, title or artists. Do not change any id. Keep the original "
              "export: import-names compares your edited copy with it, so only values you edited change.")


def _write_names(path, header, albums):
    """One track per line, so the file stays readable and diff-friendly."""
    lines = ["{"] + [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}," for k, v in header.items()]
    lines.append('  "albums": [')
    for i, a in enumerate(albums):
        head = json.dumps({k: v for k, v in a.items() if k != "tracks"}, ensure_ascii=False)[1:-1]
        lines.append("    {" + head + ', "tracks": [')
        lines.append(",\n".join("      " + json.dumps(t, ensure_ascii=False) for t in a["tracks"]))
        lines.append("    ]}" + ("," if i < len(albums) - 1 else ""))
    lines += ["  ]", "}"]
    text = "\n".join(lines) + "\n"
    json.loads(text)  # sanity check
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def cmd_export_names(args):
    jf = make_jf(args)
    albums, lib = select_albums(jf, args)
    wanted = {a["Id"] for a in albums}
    by_album = defaultdict(list)
    params = dict(IncludeItemTypes="Audio", Recursive="true", Fields="ParentId", SortBy="SortName")
    if lib:
        params["ParentId"] = lib
    print("Reading tracks from Jellyfin...", file=sys.stderr)
    for t in jf._items(**params):
        aid = t.get("AlbumId") or t.get("ParentId")
        if aid in wanted:
            by_album[aid].append(t)

    out, n_tracks = [], 0
    for a in albums:
        tracks = sorted(by_album.get(a["Id"], []), key=lambda t: (t.get("ParentIndexNumber") or 1,
                                                                  t.get("IndexNumber") or 10_000,
                                                                  (t.get("Name") or "").casefold()))
        artists = album_artist_names(a)
        multi_disc = len({t.get("ParentIndexNumber") for t in tracks if t.get("ParentIndexNumber")}) > 1
        entry = {"album_artist": artists, "album": a.get("Name") or "", "id": a["Id"], "tracks": []}
        for t in tracks:
            te = {"disc": t.get("ParentIndexNumber")} if multi_disc else {}
            te.update(track=t.get("IndexNumber"), title=t.get("Name") or "")
            track_artists = [x.get("Name") for x in t.get("ArtistItems") or [] if x.get("Name")] or t.get("Artists") or []
            if track_artists and track_artists != artists:
                te["artists"] = track_artists
            te["id"] = t["Id"]
            entry["tracks"].append(te)
        n_tracks += len(tracks)
        out.append(entry)

    size = args.split or len(out) or 1
    parts = [out[i:i + size] for i in range(0, len(out), size)] or [[]]
    base, ext = os.path.splitext(args.output)
    files = []
    for n, part in enumerate(parts, 1):
        path = args.output if len(parts) == 1 else f"{base}_part{n:02d}{ext or '.json'}"
        header = {"format": NAMES_FORMAT, "version": 1, "server": jf.url,
                  "exported": datetime.now().isoformat(timespec="seconds"),
                  "part": f"{n} of {len(parts)}", "note": NAMES_NOTE}
        _write_names(path, header, part)
        files.append(path)
    print(f"Exported {len(out)} albums and {n_tracks} tracks to {len(files)} file(s): {', '.join(files)}")
    print("Keep these originals. Edit a copy, then run: import-names <original> <edited>")


def _load_names(path):
    with open(path, encoding="utf-8-sig") as f:
        doc = json.load(f)
    if doc.get("format") != NAMES_FORMAT:
        sys.exit(f"{path} is not a file made by export-names.")
    albums, tracks = {}, {}
    for a in doc.get("albums", []):
        albums[a["id"]] = a
        for t in a.get("tracks", []):
            tracks[t["id"]] = (a["id"], t)
    return albums, tracks


def _as_list(v):
    if isinstance(v, str):
        v = [v]
    return [x.strip() for x in v or [] if isinstance(x, str) and x.strip()]


def _page_renames(pages, wanted):
    """(page id, current name, wanted name) for pages that differ from a wanted name only in
    capitalization or surrounding spaces, when no page has the exact wanted name yet."""
    by_fold = defaultdict(list)
    for pid, pname in pages.items():
        by_fold[pname.strip().casefold()].append((pid, pname))
    out = []
    for name in sorted(wanted):
        hits = by_fold.get(name.strip().casefold(), [])
        if hits and not any(pn == name for _, pn in hits) and len(hits) == 1:
            out.append((hits[0][0], hits[0][1], name))
    return out


def cmd_check_names(args):
    """Compare what Jellyfin has now with the edited files and report in plain words.
    With --fix, also plan the writes that make Jellyfin match (dry run unless --commit)."""
    files = args.edited
    want_albums, want_tracks = {}, {}
    for f in files:
        a, t = _load_names(f)
        want_albums.update(a)
        want_tracks.update(t)
    # the original exports (X.json next to X_edited.json) tell which artists were meant to be replaced
    orig_artists = {}
    for f in files:
        o = re.sub(r"_edited(\.json)$", r"\1", f)
        if o != f and os.path.exists(o):
            oa, ot = _load_names(o)
            for tid, (aid, t) in ot.items():
                orig_artists[tid] = _as_list(t.get("artists")) or _as_list(oa[aid].get("album_artist"))
    if not orig_artists:
        print("Note: original exports not found next to the edited files; extra track artists are always kept.",
              file=sys.stderr)
    jf = make_jf(args)
    print("Reading albums, tracks and artist pages from Jellyfin...", file=sys.stderr)
    albums = {a["Id"]: a for a in jf.albums()}
    tracks = {t["Id"]: t for t in jf._items(IncludeItemTypes="Audio", Recursive="true",
                                            Fields="ParentId", SortBy="SortName")}
    pages = dict(jf.artist_pages())

    def pages_for(name):
        """Pages matching a name ignoring case/spaces; searches Jellyfin directly if the list missed it."""
        fold = name.strip().casefold()
        hits = [(pid, pn) for pid, pn in pages.items() if pn.strip().casefold() == fold]
        if not hits:
            found = jf.get("/Artists", userId=jf.user_id, searchTerm=name.strip(), Limit=50).get("Items", [])
            for it in found:
                if it.get("Id") and (it.get("Name") or "").strip().casefold() == fold:
                    pages[it["Id"]] = it["Name"]
                    hits.append((it["Id"], it["Name"]))
        return hits

    page_status = {}           # name -> ("ok" | "spelled" | "missing", detail)
    def page_of(name):
        if name not in page_status:
            hits = pages_for(name)
            if any(pn == name for _, pn in hits):
                page_status[name] = ("ok", None)
            elif hits:
                page_status[name] = ("spelled", hits)
            else:
                page_status[name] = ("missing", None)
        return page_status[name]

    stats = defaultdict(Counter)
    problems, notes = defaultdict(list), defaultdict(list)
    changes, expected = defaultdict(dict), defaultdict(dict)

    for aid, wa in want_albums.items():
        label = f"{'; '.join(_as_list(wa.get('album_artist')))} - {wa.get('album')}"
        cur = albums.get(aid)
        if not cur:
            stats["albums"]["not found"] += 1
            problems[label].append("album not found in Jellyfin (removed or re-created?) - nothing to fix here")
            continue
        if (cur.get("Name") or "") == wa.get("album", ""):
            stats["album names"]["correct"] += 1
        else:
            stats["album names"]["different"] += 1
            problems[label].append(f"album name is {cur.get('Name')!r}, should be {wa.get('album')!r}")
            changes[aid]["Name"], expected[aid]["Name"] = wa.get("album", ""), cur.get("Name") or ""
        want_aa = _as_list(wa.get("album_artist"))
        shown = [x.get("Name") for x in cur.get("AlbumArtists") or [] if x.get("Name")]
        if shown == want_aa:
            stats["album artists"]["correct"] += 1
        else:
            stats["album artists"]["different"] += 1
            why = [f"no artist page for {n!r} yet" if page_of(n)[0] == "missing" else
                   f"the artist page for {n!r} is spelled {page_of(n)[1][0][1]!r}" for n in want_aa if page_of(n)[0] != "ok"]
            problems[label].append(f"album artists show as {shown}, should be {want_aa}"
                                   + (f" ({'; '.join(why)})" if why else ""))
            changes[aid]["AlbumArtists"] = want_aa
            for t in wa.get("tracks", []):
                changes[t["id"]]["AlbumArtists"] = want_aa

    for tid, (aid, wt) in want_tracks.items():
        label = f"{'; '.join(_as_list(want_albums[aid].get('album_artist')))} - {want_albums[aid].get('album')}"
        cur = tracks.get(tid)
        if not cur:
            stats["track titles"]["not found"] += 1
            problems[label].append(f"track {wt.get('title')!r} not found in Jellyfin")
            continue
        if (cur.get("Name") or "") == wt.get("title", ""):
            stats["track titles"]["correct"] += 1
        else:
            stats["track titles"]["different"] += 1
            problems[label].append(f"track title is {cur.get('Name')!r}, should be {wt.get('title')!r}")
            changes[tid]["Name"], expected[tid]["Name"] = wt.get("title", ""), cur.get("Name") or ""
        if "artists" in wt:
            want_ta, stored = _as_list(wt.get("artists")), list(cur.get("Artists") or [])
            fold = lambda xs: {x.strip().casefold() for x in xs}
            # extras: names Jellyfin has that the file doesn't. Keep those the export never saw (hidden names
            # from your music files); drop those that were in the export, because the edit replaced them.
            extras = [x for x in stored if x.strip().casefold() not in fold(want_ta)]
            replaced = [x for x in extras if x.strip().casefold() in fold(orig_artists.get(tid, []))]
            keep = [x for x in extras if x not in replaced]
            missing = [n for n in want_ta if n not in stored]
            if not missing and not replaced and stored == want_ta:
                stats["track artists"]["correct"] += 1
            elif not missing and not replaced:
                stats["track artists"]["correct, with extras"] += 1
                notes[label].append(f"{wt.get('title')!r} also lists {keep} (from your music files - fine to keep)")
            else:
                stats["track artists"]["different"] += 1
                problems[label].append(f"track {wt.get('title')!r}: artists are {stored}, should be {want_ta + keep}")
                changes[tid]["Artists"] = want_ta + keep

    wanted_names = {n for a in want_albums.values() for n in _as_list(a.get("album_artist"))}
    wanted_names |= {n for _, t in want_tracks.values() for n in _as_list(t.get("artists"))}
    spelled, missing_pages = [], []
    for n in sorted(wanted_names, key=str.casefold):
        state, hits = page_of(n)
        if state == "ok":
            stats["artist pages"]["correct"] += 1
        elif state == "spelled":
            stats["artist pages"]["spelled differently"] += 1
            spelled.append(f"{hits[0][1]!r} should be {n!r}")
            if len(hits) == 1:
                changes[hits[0][0]]["Name"], expected[hits[0][0]]["Name"] = n, hits[0][1]
        else:
            # Jellyfin may have renamed the page itself (its online artist lookup can pick another artist's
            # name). Asking for the page by name finds it; rename it back unless it is that other artist's own page.
            page = jf.artist(n)
            if page and page.get("Id") and page.get("Name") and page["Name"] != n:
                own = jf.artist(page["Name"])
                if own and own.get("Id") != page["Id"]:
                    stats["artist pages"]["renamed by Jellyfin"] += 1
                    spelled.append(f"{page['Name']!r} should be {n!r} (Jellyfin renamed this page; "
                                   f"it is not the real {page['Name']!r} page)")
                    changes[page["Id"]]["Name"], expected[page["Id"]]["Name"] = n, page["Name"]
                    continue
            stats["artist pages"]["missing"] += 1
            missing_pages.append(n)

    lines = [f"Checked {len(want_albums)} albums and {len(want_tracks)} tracks from {len(files)} file(s) "
             f"against Jellyfin.", ""]
    for key in ("album names", "album artists", "track titles", "track artists", "artist pages", "albums"):
        if stats[key]:
            lines.append(f"  {key:15} " + ", ".join(f"{v} {k}" for k, v in stats[key].items()))
    if problems:
        lines += ["", "Differences:"]
        for label, items in problems.items():
            lines.append(f"  {label}")
            lines += [f"    - {x}" for x in items]
    if spelled:
        lines += ["", "Artist pages spelled differently (--fix renames them):"] + [f"  - {x}" for x in spelled]
    if missing_pages:
        lines += ["", "Names without an artist page (Jellyfin creates pages during a library scan; "
                      "until then it hides these names):"] + [f"  - {x!r}" for x in missing_pages]
    if notes:
        lines += ["", "Notes (no action needed):"]
        for label, items in notes.items():
            lines += [f"  {label}: {x}" for x in items]
    if not problems and not spelled and not missing_pages:
        lines += ["", "Everything matches the edited files."]
    elif not args.fix and (problems or spelled):
        lines += ["", "To make Jellyfin match the files: run this again with --fix (dry run), then --fix --commit."]
    elif not problems and not spelled:
        lines += ["", "Names, titles and artists all match. Only artist pages are missing: run a library scan "
                      "(Dashboard > Scheduled Tasks > Scan Media Library), then check again."]
    report = "\n".join(lines) + "\n"
    print(report)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"(Also saved to {args.output})")

    if args.fix:
        print("\n--- Fix plan ---")
        if not changes:
            print("Nothing to write." + (" Run a library scan for the names without an artist page." if missing_pages else ""))
            return
        run_changes(jf, changes, args, expected=expected, stored_artists=True)
        if args.commit and missing_pages:
            for n in missing_pages:        # asking Jellyfin for the page lets it create one where it can
                jf.artist(n)
            print("Asked Jellyfin to create the missing artist pages; a library scan completes this.")


def cmd_lock_names(args):
    """Lock the Name field of every album and track in the edited files, so scans can't change them."""
    want_albums, want_tracks = {}, {}
    for f in args.edited:
        a, t = _load_names(f)
        want_albums.update(a)
        want_tracks.update(t)
    jf = make_jf(args)
    todo = [(aid, _as_list(a.get("album_artist"))) for aid, a in want_albums.items()]
    todo += [(tid, _as_list(want_albums[aid].get("album_artist"))) for tid, (aid, _) in want_tracks.items()]
    planned, already, missing = [], 0, 0
    started = time.monotonic()
    print(f"Looking at {len(todo)} albums and tracks...", file=sys.stderr)
    for i, (iid, album_artists) in enumerate(todo, 1):
        if i % 500 == 0:
            print(f"  [{i}/{len(todo)}]{eta_text(started, i, len(todo))}", file=sys.stderr, flush=True)
        try:
            dto = jf.item(iid)
        except RuntimeError:
            missing += 1
            continue
        if "Name" in (dto.get("LockedFields") or []) or dto.get("LockData"):
            already += 1
            continue
        planned.append((dto, album_artists))
    albums_n = sum(1 for d, _ in planned if d.get("Type") == "MusicAlbum")
    print(f"\n{len(planned)} name(s) to lock ({albums_n} albums, {len(planned) - albums_n} tracks), "
          f"{already} already locked, {missing} not found in Jellyfin.")
    if not planned:
        return
    if not args.commit:
        print("Dry run only - nothing was changed. Re-run with --commit to lock them.")
        return
    backup_path = args.backup or f"backup-locks-{datetime.now():%Y%m%d-%H%M%S}.json"
    save_json(backup_path, {"created": datetime.now().isoformat(), "server": jf.url,
                            "items": [d for d, _ in planned], "images": []})
    print(f"Backup of the original values written to {backup_path}")
    ok = failed = 0
    started = time.monotonic()
    for i, (dto, album_artists) in enumerate(planned, 1):
        new = json.loads(json.dumps(dto))
        new["LockedFields"] = sorted(set(new.get("LockedFields") or []) | {"Name"})
        # write artist lists from the files / the stored list, never from Jellyfin's view (which hides names)
        if album_artists:
            new["AlbumArtists"] = _name_pairs(album_artists, dto.get("AlbumArtists"))
        if dto.get("Type") == "Audio" and dto.get("Artists"):
            new["ArtistItems"] = _name_pairs(list(dto["Artists"]), dto.get("ArtistItems"))
        try:
            jf.update_item(new)
            ok += 1
        except Exception as e:
            failed += 1
            print(f"FAILED {dto.get('Name')!r}: {e}")
        if i % 500 == 0:
            print(f"  locked {i}/{len(planned)}{eta_text(started, i, len(planned))}", file=sys.stderr, flush=True)
    print(f"Done: {ok} locked, {failed} failed. Undo with: jfa restore {backup_path} --commit")


def cmd_import_names(args):
    orig_albums, orig_tracks = _load_names(args.original)
    new_albums, new_tracks = _load_names(args.edited)
    jf = make_jf(args)
    changes, expected, follow, unknown = defaultdict(dict), defaultdict(dict), defaultdict(dict), 0
    counts = Counter()

    for aid, na in new_albums.items():
        oa = orig_albums.get(aid)
        if not oa:
            unknown += 1
            continue
        track_ids = [t["id"] for t in oa.get("tracks", [])]
        new_name, old_name = (na.get("album") or "").strip(), oa.get("album") or ""
        if new_name and new_name != old_name:
            changes[aid]["Name"], expected[aid]["Name"] = new_name, old_name
            for tid in track_ids:
                changes[tid]["Album"] = new_name
            counts["album names"] += 1
        new_art, old_art = _as_list(na.get("album_artist")), _as_list(oa.get("album_artist"))
        if new_art and new_art != old_art:
            changes[aid]["AlbumArtists"], expected[aid]["AlbumArtists"] = new_art, old_art
            for t in oa.get("tracks", []):
                changes[t["id"]]["AlbumArtists"] = new_art
                if "artists" not in t:   # track artists matched the album artists: keep them matching
                    changes[t["id"]]["Artists"], follow[t["id"]]["Artists"] = new_art, old_art
            counts["album artists"] += 1

    for tid, (_, nt) in new_tracks.items():
        if tid not in orig_tracks:
            unknown += 1
            continue
        oaid, ot = orig_tracks[tid]
        title, old_title = (nt.get("title") or "").strip(), ot.get("title") or ""
        if title and title != old_title:
            changes[tid]["Name"], expected[tid]["Name"] = title, old_title
            counts["track titles"] += 1
        old_ta = _as_list(ot.get("artists")) or _as_list(orig_albums[oaid].get("album_artist"))
        new_ta = _as_list(nt.get("artists")) or None
        if new_ta and new_ta != old_ta:
            changes[tid]["Artists"], expected[tid]["Artists"] = new_ta, old_ta
            follow[tid].pop("Artists", None)
            counts["track artists"] += 1

    if unknown:
        print(f"Warning: {unknown} album/track id(s) in {args.edited} are not in {args.original}; ignored.")
    if not changes:
        print("The edited file has no differences from the original - nothing to do.")
        return
    # Items that get written back are written in full; make sure their artist lists come from the files
    # (complete) and not from Jellyfin's view of them (which can leave names out)
    for aid, na in new_albums.items():
        oa = orig_albums.get(aid)
        if not oa:
            continue
        old_alist = _as_list(oa.get("album_artist"))
        alist = _as_list(na.get("album_artist")) or old_alist
        if aid in changes and "AlbumArtists" not in changes[aid] and alist:
            changes[aid]["AlbumArtists"], follow[aid]["AlbumArtists"] = alist, old_alist
        for t in na.get("tracks", []):
            tid = t["id"]
            if tid not in changes or tid not in orig_tracks:
                continue
            if "AlbumArtists" not in changes[tid] and alist:
                changes[tid]["AlbumArtists"], follow[tid]["AlbumArtists"] = alist, old_alist
            # track artists need nothing here: run_changes writes back the stored list unchanged

    # Artist pages: Jellyfin may match artist names regardless of case, so its artist entry can
    # keep the old spelling ("Daft Punk") after the tracks say "Daft punk". Rename those entries too.
    wanted = {n for ch in changes.values() for key in ("AlbumArtists", "Artists") for n in ch.get(key, [])}
    renamed = 0
    if wanted:
        print(f"Checking {len(wanted)} artist name(s) against Jellyfin's artist pages...", file=sys.stderr)
        for pid, old, new in _page_renames(jf.artist_pages(), wanted):
            changes[pid]["Name"], expected[pid]["Name"] = new, old
            renamed += 1
    if renamed:
        counts["artist pages"] = renamed
    print("Edited: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + "\n")
    run_changes(jf, changes, args, expected=expected, follow=follow)

# --------------------------------------------------------------------------- cover picker


def caa_front(http, kind, mbid):
    """Full-resolution URL of the front cover of a release or release group, or None."""
    try:
        r = http.get(f"https://coverartarchive.org/{kind}/{mbid}", timeout=60,
                     headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        images = r.json().get("images", [])
    except (requests.RequestException, ValueError):
        return None
    front = (next((i for i in images if i.get("front")), None)
             or next((i for i in images if "Front" in (i.get("types") or [])), None))
    return front["image"].replace("http://", "https://", 1) if front and front.get("image") else None


def save_cover(folder, stem, data, ctype):
    from PIL import Image
    for old in glob.glob(os.path.join(folder, stem + ".*")) + glob.glob(os.path.join(folder, stem + "_thumb.*")):
        os.remove(old)
    path = os.path.join(folder, stem + IMAGE_EXT.get(ctype, ".jpg"))
    with open(path, "wb") as f:
        f.write(data)
    info = {"file": os.path.basename(path), "thumb": None, "width": None, "height": None,
            "bytes": len(data), "type": ctype}
    try:
        im = Image.open(io.BytesIO(data))
        info["width"], info["height"] = im.size
        im = im.convert("RGB")
        im.thumbnail((400, 400))
        im.save(os.path.join(folder, stem + "_thumb.jpg"), "JPEG", quality=85)
        info["thumb"] = stem + "_thumb.jpg"
    except Exception:
        pass  # unusual format: the page shows the full image instead
    return info


def fetch_album_covers(jf, mb, http, album, root):
    aid = album["Id"]
    folder = os.path.join(root, aid)
    os.makedirs(folder, exist_ok=True)
    entry = {"id": aid, "album": album.get("Name") or "", "artist": ARTIST_SEP.join(album_artist_names(album)),
             "image_tag": (album.get("ImageTags") or {}).get("Primary"), "current": None, "new": None,
             "release": None, "confidence": None, "source": None, "distance": None,
             "fetched": datetime.now().isoformat(timespec="seconds")}
    if entry["image_tag"]:
        data, ctype = jf.image(aid)
        if data:
            entry["current"] = save_cover(folder, "current", data, ctype)
        else:
            entry["image_tag"] = None
    release, conf = find_release(mb, album, jf.tracks(aid))
    if release:
        entry.update(release=release["id"], confidence=conf, release_title=release.get("title"),
                     release_artist=credit_string(release.get("artist-credit")))
        url = None
        if (release.get("cover-art-archive") or {}).get("front", True):
            url = caa_front(http, "release", release["id"])
        rg = (release.get("release-group") or {}).get("id")
        if not url and rg:
            url = caa_front(http, "release-group", rg)
        if url:
            data, ctype = fetch_cover(http, url)
            entry["new"], entry["source"] = save_cover(folder, "new", data, ctype), url
            cur, new = entry["current"], entry["new"]
            if cur and cur["thumb"] and new["thumb"]:
                with open(os.path.join(folder, cur["thumb"]), "rb") as a, open(os.path.join(folder, new["thumb"]), "rb") as b:
                    entry["distance"] = bin(dhash(a.read()) ^ dhash(b.read())).count("1")
    return entry


def cmd_covers_fetch(args):
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("covers-fetch needs Pillow: pip install pillow")
    jf = make_jf(args)
    mb = MusicBrainz(args.contact, args.cache)
    http = requests.Session()
    http.headers["User-Agent"] = mb.s.headers["User-Agent"]
    albums, _ = select_albums(jf, args)
    os.makedirs(args.dir, exist_ok=True)
    index_path = os.path.join(args.dir, "index.json")
    index = {}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    todo = [a for a in albums if args.refetch or a["Id"] not in index]
    print(f"{len(albums)} albums: {len(albums) - len(todo)} already fetched, {len(todo)} to do.", file=sys.stderr)
    started, errors = time.monotonic(), 0
    try:
        for i, album in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {album.get('AlbumArtist') or '?'} - {album.get('Name')}"
                  f"{eta_text(started, i - 1, len(todo))}", file=sys.stderr, flush=True)
            try:
                index[album["Id"]] = fetch_album_covers(jf, mb, http, album, args.dir)
            except Exception as e:   # not stored, so the next run retries it
                errors += 1
                print(f"   error: {type(e).__name__}: {e}", file=sys.stderr)
            if i % 10 == 0:
                save_json(index_path, index)
    except KeyboardInterrupt:
        print("\nInterrupted - progress is saved, run covers-fetch again to continue.", file=sys.stderr)
    finally:
        save_json(index_path, index)
        mb.save()
    entries = list(index.values())
    print(f"\n{len(entries)} albums in {args.dir}/: {sum(1 for e in entries if e['new'])} with a cover found online, "
          f"{sum(1 for e in entries if not e['current'])} without a current cover, {errors} errors this run.")
    print(f"Next: jfa covers-review   (then open the page in your browser)")


PICKER_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Pick album covers</title><style>
:root{--bg:#f5f5f3;--card:#fff;--fg:#1c1c1e;--muted:#6b6b70;--line:#e0e0dd;--accent:#2f6fde;--warn:#a35f00;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1f1f22;--fg:#ececef;--muted:#9c9ca3;--line:#34343a;--accent:#6e9dff;--warn:#e3a54a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 20px}
h1{font-size:18px;margin:0 0 4px}.help{color:var(--muted);font-size:13px;margin:0 0 10px}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.bar+.bar{margin-top:8px}
button{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;padding:5px 12px;cursor:pointer}
button.on{background:var(--accent);border-color:var(--accent);color:#fff}
input{font:inherit;font-size:14px;padding:5px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--fg);min-width:220px}
.sep{flex:1}#status{color:var(--muted);font-size:13px}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px;padding:16px 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.t{font-weight:600}.a{color:var(--muted);font-size:13px}.warn{color:var(--warn);font-size:12px;margin-top:2px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.opt{border:2px solid transparent;border-radius:10px;padding:6px;text-align:center}.opt[data-id]{cursor:pointer}
.opt.sel{border-color:var(--accent)}.opt img,.empty{width:100%;aspect-ratio:1;object-fit:contain;background:var(--bg);border-radius:6px;display:block}
.empty{display:grid;place-items:center;color:var(--muted);font-size:13px}
.lbl{font-size:12px;color:var(--muted);margin-top:5px}.lbl b{color:var(--fg)}a{color:var(--accent)}
</style></head><body>
<header><h1>Pick album covers</h1>
<p class="help">Click the cover you want for each album. Every album keeps its current cover unless you choose the new one. Choices are saved as you click.</p>
<div class="bar" id="filters"></div>
<div class="bar"><input type="search" id="q" placeholder="Search album or artist">
<button id="allNew">Use new for all shown</button><button id="allCur">Keep current for all shown</button>
<span class="sep"></span><span id="status"></span></div></header>
<main id="grid"></main>
<script>
const CATS=[["different","Different art"],["missing","No cover yet"],["better","Same art, higher resolution"],["same","Same art"],["none","Nothing found online"],["chosen","Chosen: new"],["all","All"]];
let data={albums:[]},choices={},filter="different",q="";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function cat(a){if(!a.new)return"none";if(!a.current)return"missing";
 if(a.distance!=null&&a.distance>data.threshold)return"different";
 const pc=(a.current.width||0)*(a.current.height||0),pn=(a.new.width||0)*(a.new.height||0);return pn>1.5*pc?"better":"same"}
function size(i){const b=i.bytes,s=b>=1048576?(b/1048576).toFixed(1)+" MB":Math.max(1,Math.round(b/1024))+" KB";return i.width?`${i.width}×${i.height} · ${s}`:s}
function opt(a,which){const i=a[which],sel=(choices[a.id]||"current")===which,label=which==="new"?"Use new":"Keep current";
 const click=(which==="current"||i)?` data-id="${a.id}" data-c="${which}"`:"";
 const pic=i?`<img loading="lazy" src="/img/${a.id}/${i.thumb||i.file}" alt="">`:`<div class="empty">${which==="new"?"Nothing found online":"No cover"}</div>`;
 const info=i?`<br>${size(i)} · <a href="/img/${a.id}/${i.file}" target="_blank">full size</a>`:"";
 return `<div class="opt${sel?" sel":""}"${click}>${pic}<div class="lbl"><b>${sel?"✓ ":""}${label}</b>${info}</div></div>`}
function card(a){const low=a.confidence&&!["mbid","high"].includes(a.confidence);
 const warn=low?`<div class="warn">Uncertain match (${a.confidence}): MusicBrainz has “${esc(a.release_title)}” by ${esc(a.release_artist)}</div>`:"";
 return `<div class="card" id="c-${a.id}"><div class="t">${esc(a.album)}</div><div class="a">${esc(a.artist)}</div>${warn}<div class="pair">${opt(a,"current")}${opt(a,"new")}</div></div>`}
function shown(){return data.albums.filter(a=>(filter==="all"||(filter==="chosen"?choices[a.id]==="new":cat(a)===filter))
 &&(!q||(a.album+" "+a.artist).toLowerCase().includes(q)))}
function counts(){const c={all:data.albums.length,chosen:Object.keys(choices).length};for(const a of data.albums)c[cat(a)]=(c[cat(a)]||0)+1;
 $("#filters").innerHTML=CATS.map(([k,l])=>`<button data-f="${k}" class="${k===filter?"on":""}">${l} (${c[k]||0})</button>`).join("")}
function render(){counts();const list=shown();$("#grid").innerHTML=list.length?list.map(card).join(""):"<p>No albums in this view.</p>"}
async function save(update,full){for(const[k,v]of Object.entries(update)){if(v==="new")choices[k]="new";else delete choices[k]}
 if(full)render();else{counts();for(const id of Object.keys(update)){const el=document.getElementById("c-"+id),a=data.albums.find(x=>x.id===id);if(el&&a)el.outerHTML=card(a)}}
 $("#status").textContent="Saving…";
 try{const r=await fetch("/api/choices",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(update)});
  if(!r.ok)throw new Error(r.status);const d=await r.json();$("#status").textContent=`Saved · ${d.new} album(s) will get the new cover`}
 catch(e){$("#status").textContent="Not saved - is covers-review still running?"}}
$("#grid").addEventListener("click",e=>{if(e.target.closest("a"))return;const o=e.target.closest(".opt[data-id]");if(o)save({[o.dataset.id]:o.dataset.c})});
$("#filters").addEventListener("click",e=>{const b=e.target.closest("button[data-f]");if(b){filter=b.dataset.f;render();scrollTo(0,0)}});
$("#q").addEventListener("input",e=>{q=e.target.value.trim().toLowerCase();render()});
function bulk(choice){const ids=shown().filter(a=>choice!=="new"||a.new).map(a=>a.id);if(!ids.length)return;
 const what=choice==="new"?"use the new cover":"keep the current cover";
 if(confirm(`Set ${ids.length} album(s) to ${what}?`))save(Object.fromEntries(ids.map(i=>[i,choice])),true)}
$("#allNew").onclick=()=>bulk("new");$("#allCur").onclick=()=>bulk("current");
fetch("/api/albums").then(r=>r.json()).then(d=>{data=d;choices=d.choices||{};
 if(!data.albums.some(a=>cat(a)==="different"))filter=data.albums.some(a=>cat(a)==="missing")?"missing":"all";render()})
 .catch(()=>{$("#grid").innerHTML="<p>Could not load the album list.</p>"});
</script></body></html>"""


def cmd_covers_review(args):
    index_path = os.path.join(args.dir, "index.json")
    if not os.path.exists(index_path):
        sys.exit(f"{index_path} not found - run covers-fetch first.")
    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)
    choices_path = os.path.join(args.dir, "choices.json")
    choices = {}
    if os.path.exists(choices_path):
        with open(choices_path, encoding="utf-8") as f:
            choices = {k: v for k, v in json.load(f).items() if v == "new" and k in index}
    albums = sorted(index.values(), key=lambda e: (e["artist"].casefold(), e["album"].casefold()))
    lock = threading.Lock()
    root = os.path.abspath(args.dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, cache=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if cache:
                self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                return self._send(200, PICKER_PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/albums":
                with lock:
                    body = json.dumps({"threshold": args.cover_threshold, "albums": albums, "choices": choices})
                return self._send(200, body.encode(), "application/json")
            m = re.fullmatch(r"/img/([0-9a-f]+)/([\w.]+)", path)
            if m and m.group(1) in index:
                e = index[m.group(1)]
                allowed = {v for side in ("current", "new") if e.get(side) for v in (e[side]["file"], e[side]["thumb"]) if v}
                if m.group(2) in allowed:
                    file = os.path.join(root, m.group(1), m.group(2))
                    with open(file, "rb") as f:
                        ctype = EXT_TYPE.get(os.path.splitext(file)[1].lower(), "application/octet-stream")
                        return self._send(200, f.read(), ctype, cache=True)
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/api/choices":
                return self._send(404, b"not found", "text/plain")
            try:
                update = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                return self._send(400, b"bad json", "text/plain")
            with lock:
                for aid, c in update.items():
                    if aid in index and c == "new" and index[aid].get("new"):
                        choices[aid] = "new"
                    elif aid in index:
                        choices.pop(aid, None)
                save_json(choices_path, choices)
                body = json.dumps({"new": len(choices)})
            self._send(200, body.encode(), "application/json")

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    host = socket.gethostname()
    print(f"Cover picker running. Open http://{host}:{args.port}/ (or http://<server-ip>:{args.port}/) "
          f"in your browser.\nChoices are saved to {choices_path} as you click. Press Ctrl+C here when done.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print(f"\nStopped. {len(choices)} album(s) set to get the new cover. "
          f"Next: jfa covers-apply")


def cmd_covers_apply(args):
    with open(os.path.join(args.dir, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    choices_path = os.path.join(args.dir, "choices.json")
    choices = {}
    if os.path.exists(choices_path):
        with open(choices_path, encoding="utf-8") as f:
            choices = json.load(f)
    covers = {}
    for aid, c in choices.items():
        e = index.get(aid)
        if c != "new" or not e or not e.get("new"):
            continue
        folder = os.path.join(args.dir, aid)
        covers[aid] = {"src": os.path.join(folder, e["new"]["file"]), "expect_tag": e.get("image_tag"),
                       "backup_file": os.path.join(folder, e["current"]["file"]) if e.get("current") else None,
                       "label": f"{e['artist']} - {e['album']}"}
    if not covers:
        print("No album is set to get the new cover - pick some with covers-review first.")
        return
    run_changes(make_jf(args), {}, args, covers=covers)

# --------------------------------------------------------------------------- CLI


def make_jf(args):
    url = args.server or os.environ.get("JELLYFIN_URL")
    key = args.api_key or os.environ.get("JELLYFIN_API_KEY")
    if not url or not key:
        sys.exit("Set JELLYFIN_URL and JELLYFIN_API_KEY (or pass --server and --api-key).")
    return Jellyfin(url, key, args.user)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", help="Jellyfin URL (default: $JELLYFIN_URL)")
    common.add_argument("--api-key", help="Jellyfin API key (default: $JELLYFIN_API_KEY)")
    common.add_argument("--user", help="Jellyfin user to query as (default: first administrator)")
    common.add_argument("--contact", default=os.environ.get("MB_CONTACT", "no-contact-given"),
                        help="Email/URL sent to MusicBrainz in the User-Agent (default: $MB_CONTACT)")

    writing = argparse.ArgumentParser(add_help=False)
    writing.add_argument("--commit", action="store_true", help="actually write changes (default is a dry run)")
    writing.add_argument("--no-lock", action="store_true",
                         help="don't lock the Name field of renamed items against metadata refreshes")
    writing.add_argument("--lock-item", action="store_true",
                         help="lock changed items completely (protects artists from refreshes too)")
    writing.add_argument("--backup", help="backup file name (default: backup-<timestamp>.json)")

    picking = argparse.ArgumentParser(add_help=False)
    picking.add_argument("--library", help="only this music library (by name)")
    picking.add_argument("--album", help="only albums whose name or artist contains this text")
    picking.add_argument("--limit", type=int, help="stop after N albums (handy for a first test)")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("audit", parents=[common, picking], help="compare Jellyfin with MusicBrainz, write a CSV")
    a.add_argument("-o", "--output", default="jellyfin_music_report.csv")
    a.add_argument("--straight-quotes", action="store_true",
                   help="propose plain ' \" - instead of MusicBrainz's typographic ’ “ ‐")
    a.add_argument("--compare-covers", action="store_true",
                   help="also flag existing covers that look different from MusicBrainz (needs Pillow)")
    a.add_argument("--cover-threshold", type=int, default=12,
                   help="image-hash distance (0-64) above which covers count as different (default 12)")
    a.add_argument("--sentence-case", metavar="FIELDS",
                   help="comma list of track,album,artist (or all): capitalize only the first word "
                        "and words that need it, e.g. 'Wake me up when September ends'")
    a.add_argument("--keep-caps", metavar="FILE",
                   help="words/phrases that keep their capitals with --sentence-case "
                        "(default: keep_caps.txt if it exists)")
    a.add_argument("--cache", default=".musicbrainz_cache.json",
                   help="cache file for MusicBrainz answers so re-runs are fast ('' to disable)")
    a.set_defaults(func=cmd_audit)

    ap = sub.add_parser("apply", parents=[common, writing], help="push approved fixes from the audit CSV to Jellyfin")
    ap.add_argument("report")
    ap.set_defaults(func=cmd_apply)

    en = sub.add_parser("export-names", parents=[common, picking],
                        help="write album names, artists and track titles to JSON for editing")
    en.add_argument("-o", "--output", default="jellyfin_names.json")
    en.add_argument("--split", type=int, metavar="N", help="N albums per file (e.g. 250) for easier review")
    en.set_defaults(func=cmd_export_names)

    im = sub.add_parser("import-names", parents=[common, writing],
                        help="apply the values you edited in an export-names file")
    im.add_argument("original", help="the file export-names wrote")
    im.add_argument("edited", help="your edited copy of it")
    im.set_defaults(func=cmd_import_names)

    ck = sub.add_parser("check-names", parents=[common, writing],
                        help="check that Jellyfin has the names from your edited files (--fix to repair)")
    ck.add_argument("edited", nargs="+", help="one or more edited files")
    ck.add_argument("--fix", action="store_true",
                    help="also plan the writes that make Jellyfin match the files (dry run unless --commit)")
    ck.add_argument("-o", "--output", default="check_names.txt")
    ck.set_defaults(func=cmd_check_names)

    lk = sub.add_parser("lock-names", parents=[common, writing],
                        help="lock the name of every album and track in the edited files against scans")
    lk.add_argument("edited", nargs="+", help="one or more edited files")
    lk.set_defaults(func=cmd_lock_names)

    cf = sub.add_parser("covers-fetch", parents=[common, picking],
                        help="download current covers and the best cover online for every album")
    cf.add_argument("--dir", default="covers", help="where to keep the images (default: covers)")
    cf.add_argument("--refetch", action="store_true", help="fetch albums again even if already done")
    cf.add_argument("--cache", default=".musicbrainz_cache.json", help="MusicBrainz cache file ('' to disable)")
    cf.set_defaults(func=cmd_covers_fetch)

    cr = sub.add_parser("covers-review", parents=[common], help="web page to pick current or new cover per album")
    cr.add_argument("--dir", default="covers")
    cr.add_argument("--port", type=int, default=8765)
    cr.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all)")
    cr.add_argument("--cover-threshold", type=int, default=12,
                    help="image-hash distance (0-64) above which art counts as different (default 12)")
    cr.set_defaults(func=cmd_covers_review)

    ca = sub.add_parser("covers-apply", parents=[common, writing], help="upload the covers you picked")
    ca.add_argument("--dir", default="covers")
    ca.set_defaults(func=cmd_covers_apply)

    r = sub.add_parser("restore", parents=[common], help="undo an apply/import/covers-apply using its backup")
    r.add_argument("backup")
    r.add_argument("--commit", action="store_true")
    r.set_defaults(func=cmd_restore)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
