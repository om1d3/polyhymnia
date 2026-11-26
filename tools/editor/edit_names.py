#!/usr/bin/env python3
"""edit_names.py - edit Polyhymnia export files in the style of the library.

Use:
  python3 edit_names.py new_albums_20261126-0900.json
  python3 edit_names.py jellyfin_names_part0*.json -o edited/

For each input file, the tool writes <name>_edited.json next to it (or in the -o folder).
It also writes a CSV file with all changes for review. The input files do not change.
The rules are in rules.py.
"""
import argparse, csv, collections, glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules import *   # noqa: F401,F403

NAMES_FORMAT = "jellyfin-music-names"

# ------------------------------------------------------------ classification of titles
MINOR = set("a an the and but or nor for so yet of in on at to by up as from with into onto upon over than via vs per off out feat ft & n' 'n' n de la le les el del da di du des et y e o und der die das den dem zu im am vom zum mit".split())
def words(s):
    return re.findall(r"[^\W\d_][^\s/()\[\]{}:;,.!?\"“”]*", s)
def classify(s):
    letters = [c for c in s if c.isalpha() and c.lower() != c.upper()]
    if not letters: return "nocase"
    if all(c.isupper() for c in letters):
        return "allcaps" if len(letters) > 1 else "single"
    if all(c.islower() for c in letters): return "lower"
    # content words after the first word
    toks = re.split(r"\s+", s.strip())
    content, start = [], True
    for tok in toks:
        core = re.sub(r"^\W+|\W+$", "", tok)
        if not core or not any(c.isalpha() for c in core):
            if re.fullmatch(r"[-–—/|:]+", tok): start = True
            continue
        if start:
            start = bool(re.search(r"[.!?:]$", tok)); continue
        start = bool(re.search(r"[.!?:]$", tok)) and core.lower().rstrip(".") not in {"feat","ft","vs","vol","no","op","pt","nr"}
        if tok.startswith(("(", "[")):  # first word inside brackets: ignore for detection
            continue
        if core.casefold() in MINOR or len(core) < 2: continue
        if core.isupper(): continue          # acronym
        if any(c.isdigit() for c in core): continue
        content.append(core)
    if not content: return "short"
    lower = [w for w in content if w[0].islower()]
    if not lower: return "titlecase"
    return "sentence"

# ------------------------------------------------------------ artist names
def artist_names(albums):
    names = set()
    for a in albums:
        raw = list(a["album_artist"]) + [x for t in a["tracks"] for x in t.get("artists", [])]
        for r in raw:
            for part in re.split(r"\s*(?:;|,|&|/|\band\b|\bfeat\.?|\bft\.|\bwith\b|\bvs\.?|\bmeets\b)\s*", r, flags=re.I):
                part = re.sub(r"\(.*?\)", "", part).strip(" .-")
                if len(part) >= 2 and any(c.isalpha() for c in part):
                    names.add(part)
    return names

# ------------------------------------------------------------ text repair
def fix_text(s):
    s = s.replace("\u200e", "").replace("\u200f", "")
    if re.search(r"[ÂÃÄÅ][\x80-\xbf¡-¿]|â\x80", s):
        try: s = s.encode("latin-1").decode("utf-8")
        except UnicodeError: pass
    s = s.translate({0x9a: "š", 0x9e: "ž", 0x8a: "Š", 0x8e: "Ž"})
    # Windows-1251 glitches inside Latin words (gehцrt -> gehört)
    if re.search(r"[A-Za-z][\u0400-\u04FF]|[\u0400-\u04FF][A-Za-z]", s):
        s = re.sub(r"(?<=[A-Za-z])[\u0400-\u04FF]|[\u0400-\u04FF](?=[A-Za-z])",
                   lambda m: m.group(0).encode("cp1251").decode("latin-1"), s)
    s = s.replace("Womens´s", "Women's")
    return re.sub(r"\s{2,}", " ", s).strip()

# ------------------------------------------------------------ sentence case
def build_protector2(phrases):
    phrases = sorted({p for p in phrases if p}, key=len, reverse=True)
    the = [p[4:] for p in phrases if p.startswith("the ")]          # "the King": protect only "King"
    rest = [p for p in phrases if not p.startswith("the ")]
    alt = "|".join(re.escape(p) for p in rest)
    if the:
        alt = r"(?<=\b[Tt]he )(?:" + "|".join(re.escape(p) for p in the) + r")|" + alt
    return re.compile(r"(?<!\w)(?<!\w['’])(?:" + alt + r")(?!\w)(?!['’]\w)", re.I)

FIRST = set("""John Paul George Ringo Mark David Michael Peter Eric Bob Bill Billy Jimmy Jimi Johnny Tom Tommy Joe Jack James
Mary Maria Anna Marie Elvis Frank Dean Ray Sam Steve Stevie Chuck Muddy Buddy Robert Richard Rick Mick Keith Charlie Ronnie
Dave Neil Phil Freddie Brian Roger Nick Lou Iggy Patti Janis Aretha Etta Nina Billie Ella Louis Miles Ian Alan Chris Kate
Jane Lucy Sally Suzanne Leonard Woody Hank Willie Dolly Kenny Bruce Marvin Smokey Al Otis Ike Tina Ben Ed Edward Harry Henry
Fred Ted Tim Andy Dan Ion Mircea Vasile Gheorghe Nicolae Mihai Andrei Alexandru Florian Gelu Grigore Tudor Nichita Lewis Kurt
Victor Arthur Oscar Walt Pablo Carlos Juan Jose Luis Luigi Giuseppe Antonio Giovanni Wolfgang Ludwig Johann Franz Igor Sergei
Ivan Sonny Jim Gene Glen Glenn Buck Carl Clint Dylan Elton Gary Jerry Jeff Larry Lenny Marty Mike Ozzy Pete Sid Syd Tony Vince
Marilyn Madonna Ruby Rosa Ana Elena Ioana Mihaela Corina Laura Lisa Lola Lily Rita Ruth Sarah Sophie Emily Emma Alice Ann
Annie Bobby Danny Eddie Frankie Jackie Jenny Joey Kelly Lenny Maggie Molly Nancy Peggy Polly Ricky Rocky Rosie Sadie Sandy
Susie Terry Tommy Vicky Wendy Cecilia Delilah Gloria Jolene Layla Roxanne Valerie Veronica Virginia Angie Daisy Josephine
Sandra Linda Barbara Patricia Christine Caroline Natalie Juliet Romeo Hugo Dorian Simon Albert Alfred Bernard Charles Daniel
Francis Frederick Gordon Howard Isaac Jacob Joseph Kenneth Lawrence Martin Matthew Nathan Oliver Patrick Raymond Samuel Thomas
Walter William Zach Vanessa Helena Émile Frances Mylène Jules Jean Pierre Jacques Serge Claude Marcel Franco Paolo Marco Luca
Ennio Nino Dino""".split())
COMMON2 = set("""And Or But Is Was Are Were Be Been Being Am Do Does Did Done Have Has Had Will Would Can Could Should Shall May
Might Must Not No Yes Of In On At To For From With By About After Before Into Over Under Up Down Out Off Again Really Very
Just Only Also Too So Then Than Now Here There When Where Why How What Who Which That This These Those The A An My Your His
Her Its Our Their Me You Him Us Them I It We They All Any Some Every Each Got Get Gets Said Says Say Goes Go Went Come Comes
Came Know Knows Like Love Loves Wants Want Needs Need Song Songs Blues Theme Mix Remix Version Live Demo""".split())
NAME_PAIR = re.compile(r"(?<!\w)(?:" + "|".join(sorted(FIRST, key=len, reverse=True)) + r")\s+(?:[A-Z]\.\s+)?"
                       r"(?!(?:" + "|".join(sorted(COMMON2)) + r")(?![\w'’]))[A-ZÀ-ÖØ-Þ][\w'’-]+")
TITLED = re.compile(r"(?<!\w)(?:St\.?|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Saint|Sir|Captain|Uncle|Aunt|Doctor)\s+[A-Z][\w'’-]+")
NO_BREAK = {"blvd", "st", "mr", "mrs", "ms", "dr", "inc", "feat", "ft", "vs", "vol", "no", "nr", "pt", "op", "ca", "aka",
            "posth", "arr", "transcr", "instr"}
NAMES_FOLLOW = {"feat", "ft", "featuring", "vs", "f"}
LOWER_SINGLE = set("AOEYUÀÉ")          # articles/conjunctions in several languages
DESCRIPTORS = set("""live demo demos remastered remaster remasters remix remixed remixes mix mixes edit edited version versions
single bonus b-side rehearsal take disc cd extended radio album alternate alternative original instrumental acoustic
unplugged outtake outtakes session sessions reprise intro outro interlude explicit clean mono stereo deluxe edition digital
special limited anniversary expanded rework recorded unreleased early rough studio home ep lp maxi promo short long full
part pt vol volume soundtrack ost theme main end credits orchestral piano vocal vocals from with performed feat featuring
remastered/live dub club rmx instr demo/live inst super slow fast electric unlisted hidden new old""".split())
QUOTES = "\"“'‘„«"
MOVEMENT = re.compile(r"I|II|III|IV|V|VI|VII|VIII|IX|X")

def case_core(core, start, trail, bracket_first, quote_first, last):
    if re.fullmatch(r"I(['’]\w+)?", core): return core
    if any(c.isdigit() for c in core):
        m = re.match(r"(?i)(no|nr|op|vol|pt)\.(.*)", core)
        return (m.group(1).lower() + "." + m.group(2)) if (m and not start) else core
    if len(core) > 1 and core.isupper(): return core                      # USA, DJ, II, EP
    if any(a.islower() and b.isupper() for a, b in zip(core, core[1:])): return core   # McCartney
    if core.casefold() == "o'clock": return "O'clock" if start else "o'clock"
    if re.match(r"[A-Z]['’][A-Z]", core): return core                   # O'Brien
    if len(core) == 1 and core.isupper():
        if trail.startswith(".") or last or core not in LOWER_SINGLE: return core     # initials, Kid A, Plan B
    if re.match(r"[A-Z]-\w", core): return core[:2] + core[2:].lower()   # B-side, T-birds
    if bracket_first:
        if core.casefold() in DESCRIPTORS: return core.lower()          # (live), (demo), (remastered)
        return core                                                      # subtitle in brackets
    if quote_first: return core
    low = core.lower()
    return low[:1].upper() + low[1:] if start else low

KEY = re.compile(r"(?<![\w])[A-G](?:#|b|♯|♭)?(?=(?:-|\s)(?i:sharp|flat|major|minor|dur|moll)\b)"
                 r"|(?<=\bin )[A-G](?:#|b|♯|♭)?m?(?=$|\s*[(\[),;]|\s+-\s)|(?<![\w])[A-G](?:#|b)m(?![\w])")
INSIDE = re.compile(r"(?i)[(\[][^)\]]*?\b(?:live (?:at|in|from)|recorded (?:at|in)|concert (?:at|in)|at|by)\s+([^)\]]+)")

def sentence(s, protector):
    out, start, names, depth, group_new_phrase, after_colon = [], True, False, 0, False, False
    toks = re.split(r"(\s+)", s)
    words = [i for i, t in enumerate(toks) if t and not t.isspace() and re.search(r"\w", t)]
    for i, tok in enumerate(toks):
        if not tok or tok.isspace():
            out.append(tok); continue
        lead, core, trail = re.match(r"^(\W*)(.*?)(\W*)$", tok, re.S).groups()
        if not core:
            out.append(tok)
            depth += tok.count("(") + tok.count("[") - tok.count(")") - tok.count("]")
            if re.fullmatch(r"[-–—/|:]+", tok): start = True
            if re.fullmatch(r"[(\[][&+]", tok) or (tok in ("&", "+") and out and re.search(r"[(\[]\s*$", "".join(out))): names = True
            continue
        prev = re.sub(r"\W", "", toks[words[words.index(i) - 1]]).casefold() if words and words.index(i) > 0 else ""
        last = (bool(words) and i == words[-1]) or prev in {"side", "part", "plan", "vitamin", "disc", "type", "track", "take", "cd", "kid"}
        bracket_first = bool(re.search(r"[(\[]", lead))
        quote_first = any(q in lead for q in QUOTES)
        if names and bracket_first: names = False          # a new bracket ends a list of names
        if names:
            new = core
        else:
            new = case_core(core, start, trail, bracket_first and i != words[0], quote_first, last)
        parts_src, parts_new = core.split("/"), new.split("/")    # Manifest/Outro keeps its second capital
        if len(parts_src) == len(parts_new) > 1:
            new = "/".join([parts_new[0]] + [ps[:1] + pn[1:] for ps, pn in zip(parts_src[1:], parts_new[1:])])
        out.append(lead + new + trail)
        key = core.casefold().rstrip(".")
        if bracket_first and (i == words[0] or key == "from"): group_new_phrase = True
        depth += lead.count("(") + lead.count("[") - trail.count(")") - trail.count("]")
        if key in NAMES_FOLLOW and (key != "f" or (core == "f" and trail.startswith("."))): names = True
        elif names and re.search(r"[)\]]", trail): names = False
        start = (bool(re.search(r"[.!?:]", trail)) and key not in NO_BREAK
                 and not re.fullmatch(r"(?:[A-Za-z]\.)*[A-Za-z]", core))
        if MOVEMENT.fullmatch(core) and (trail.startswith(".") or after_colon): start = True   # "V. Menuet"
        after_colon = trail.endswith(":")
        if group_new_phrase and depth <= 0 and re.search(r"[)\]]", trail):     # "(Looking for) The heart..."
            start, group_new_phrase = True, False
    res = list("".join(out))
    if len(res) != len(s): return "".join(res)
    spans = [m.span() for m in protector.finditer(s)] + [m.span() for m in KEY.finditer(s)]
    spans += [m.span() for m in NAME_PAIR.finditer(s)] + [m.span() for m in TITLED.finditer(s)]
    spans += [m.span(1) for m in INSIDE.finditer(s)]
    for a, b in spans:
        res[a:b] = s[a:b]
    first = next((i for i, c in enumerate(s) if c.isalpha()), -1)
    for i, c in enumerate(s):          # never capitalize a letter you wrote in lowercase...
        if c.islower() and res[i].isupper() and i != first:
            res[i] = c
    for m in re.finditer(r"(?<![^\W\d_])I(?![^\W\d_])", s):   # ...and keep the pronoun I, even in "Proof...I"
        res[m.start()] = "I"
    out = "".join(res)
    out = re.sub(r"(?<![\w'’])(?i:dj|tv|uk|usa)(?![\w'’])", lambda m: m.group(0).upper(), out)
    out = re.sub(r"(?<=\w )(?:ii|iii|iv|vii|viii|Ii|Iii|Iv|Vii|Viii)(?![\w'’])", lambda m: m.group(0).upper(), out)
    return out

BAND_RE = re.compile(r"(?<![\w'’])(?:" + "|".join(re.escape(k) for k in sorted(set(BAND_MAP) - EXACT_ONLY, key=len, reverse=True)) + r")(?![\w'’])")

def band_style(x):
    if x in BAND_MAP: return BAND_MAP[x]
    return BAND_RE.sub(lambda m: BAND_MAP[m.group(0)], x)

def fix_artist(x):
    x = fix_text(x)
    x = ARTIST_FIX.get(x, x)
    x = band_style(x)
    m = re.match(r"^\d{2} (\S.*)$", x)
    if m: x = NUMBERED_ARTIST.get(m.group(1), m.group(1))
    m = re.match(r"^(Laibach) - \d{4}$", x)
    if m: x = m.group(1)
    return x

def dedupe(lst):
    out = []
    for x in lst:
        if x and x not in out: out.append(x)
    return out


def write_names(path, header, albums):
    """Write one track on each line, in the same format as Polyhymnia."""
    lines = ["{"] + [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}," for k, v in header.items()]
    lines.append('  "albums": [')
    for i, a in enumerate(albums):
        head = json.dumps({k: v for k, v in a.items() if k != "tracks"}, ensure_ascii=False)[1:-1]
        lines.append("    {" + head + ', "tracks": [')
        lines.append(",\n".join("      " + json.dumps(t, ensure_ascii=False) for t in a["tracks"]))
        lines.append("    ]}" + ("," if i < len(albums) - 1 else ""))
    lines += ["  ]", "}"]
    text = "\n".join(lines) + "\n"
    json.loads(text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def edit_files(paths, out_dir=None):
    docs = []
    for p in paths:
        with open(p, encoding="utf-8-sig") as f:
            doc = json.load(f)
        if doc.get("format") != NAMES_FORMAT:
            sys.exit(f"{p} is not an export file from Polyhymnia.")
        docs.append((p, doc))
    albums = [a for _, d in docs for a in d.get("albums", [])]
    names = {re.sub(r"^\d+\s+", "", n) for n in artist_names(albums)} | set(LIBRARY_ARTISTS)
    prot = {n for n in names if (" " in n and n.casefold() not in COMMON_MULTI) or (" " not in n and n not in COMMON_SINGLE)}
    prot |= KEEP | KEEP2 | set(PHRASES)
    P = build_protector2(prot)
    report = []

    def album_prefix(a):
        ts = [t["title"] for t in a["tracks"]]
        for pat in (r"^\d{2}\. ", r"^\(\d{2}\) \[[^\]]+\] "):
            if ts and sum(bool(re.match(pat, t)) for t in ts) >= max(2, 0.8 * len(ts)):
                return pat
        return None

    def title_fix(s, album_name, artists, album_pat=None):
        orig = s
        s = fix_text(s)
        reason = "encoding/spaces" if s != orig else ""
        if album_name in PREFIX_ALBUMS:
            for p in PREFIXES:
                t = re.sub(p, "", s)
                if t != s and t.strip():
                    s, reason = t, "file-name leftovers"; break
        if album_pat:
            t = re.sub(album_pat, "", s)
            if t != s and t.strip(): s, reason = t, "file-name leftovers"
        if s in TITLE_FIX:
            return TITLE_FIX[s], "by hand"
        if fix_text(orig) in TITLE_FIX:
            return TITLE_FIX[fix_text(orig)], "by hand"
        if s.casefold() in {x.casefold() for x in artists} or s in ("xx",):
            return s, reason
        for p, r in TYPO:
            t = re.sub(p, r, s)
            if t != s: s, reason = t, "typo"
        if set(artists) & NO_CASE_ALBUM_ARTISTS or album_name in NO_CASE_ALBUMS:
            return s, reason
        c = classify(s)
        if c in ("titlecase", "short") or reason == "file-name leftovers":
            t = sentence(s, P)
            if t != s: s, reason = t, reason or "capitalization"
        elif c == "lower" and s[:1].isalpha() and not re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", s):
            t = s[0].upper() + s[1:]
            t = STEPA.get(t, t)
            if t != s: s, reason = t, reason or "capitalization"
        return s, reason

    written = []
    for path, doc in docs:
        text_head = {k: v for k, v in doc.items() if k != "albums"}
        for a in doc["albums"]:
            old_album, old_art = a["album"], list(a["album_artist"])
            # album artist
            new_art = dedupe([fix_artist(x) for x in a["album_artist"]])
            if old_album == "Virgula" and all(re.fullmatch(r"\d+", x) for x in old_art):
                new_art = ["Alternosfera"]
            if not new_art and old_album in ALBUM_ARTIST_FILL:
                new_art = [band_style(x) for x in ALBUM_ARTIST_FILL[old_album]]
            if new_art != old_art:
                a["album_artist"] = new_art
                report.append(("album artist", "; ".join(old_art) or "(empty)", "; ".join(new_art), old_album, "filled in" if not old_art else "fixed"))
            # album name
            if old_album in ALBUM_FIX:
                na, why = ALBUM_FIX[old_album], "typo/consistency"
            else:
                na, why = title_fix(old_album, None, new_art)
            for band in old_art:                       # self-titled albums follow the band style
                if band in BAND_MAP and band not in EXACT_ONLY:
                    m = re.match(r"(?i)(the )?" + re.escape(band) + r"(?![\w'’])", na)
                    if m and (m.group(1) is None or m.group(0).casefold() == ("the " + band).casefold()):
                        styled = BAND_MAP[band] if not m.group(1) else m.group(1) + BAND_MAP[band][:1].lower() + BAND_MAP[band][1:]
                        if not m.group(1): styled = styled
                        new_na = styled + na[m.end():]
                        if m.group(1): new_na = new_na[:1].upper() + new_na[1:]
                        if new_na != na: na, why = new_na, why or "band name style"
            if na != old_album:
                a["album"] = na
                report.append(("album", old_album, na, "; ".join(new_art), why))
            # tracks
            pat = album_prefix(a)
            for t in a["tracks"]:
                nt, why = title_fix(t["title"], old_album, new_art, pat)
                if old_album == "Virgula" and new_art == ["Alternosfera"] and t.get("track") in VIRGULA:
                    nt, why = VIRGULA[t["track"]], "diacritics (official track list)"
                    if t.get("artists") and all(re.fullmatch(r"\d+", x) for x in t["artists"]):
                        report.append(("track artist", "; ".join(t["artists"]), "Alternosfera", f"{na} / {nt}", "fixed"))
                        t["artists"] = ["Alternosfera"]
                if old_album == "muzica de colectie vol57 (jurnalul national)" and t.get("track") in STEPA57:
                    nt, why = STEPA57[t["track"]], "diacritics (official track list)"
                if nt != t["title"]:
                    report.append(("title", t["title"], nt, f"{'; '.join(new_art)} - {na}", why))
                    t["title"] = nt
                if "artists" in t:
                    na2 = dedupe([fix_artist(x) for x in t["artists"]])
                    if na2 != t["artists"]:
                        report.append(("track artist", "; ".join(t["artists"]), "; ".join(na2), f"{na} / {nt}", "fixed"))
                        t["artists"] = na2
        base = os.path.basename(path)[:-5] + "_edited.json"
        out = os.path.join(out_dir or os.path.dirname(path) or ".", base)
        write_names(out, text_head, doc["albums"])
        written.append(out)
    return written, report


def main():
    ap = argparse.ArgumentParser(description="Edit Polyhymnia export files in the style of the library.")
    ap.add_argument("files", nargs="+", help="export files from 'jfa export-names' (not the _edited files)")
    ap.add_argument("-o", "--out-dir", help="folder for the edited files (default: next to each input file)")
    ap.add_argument("--report", help="CSV file with all changes (default: changes_for_review.csv in the output folder)")
    args = ap.parse_args()
    files = [f for f in args.files if not f.endswith("_edited.json")]
    if not files:
        sys.exit("Give the original export files, not the _edited files.")
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
    written, report = edit_files(files, args.out_dir)
    rep = args.report or os.path.join(args.out_dir or os.path.dirname(files[0]) or ".", "changes_for_review.csv")
    with open(rep, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["what", "why", "before", "after", "where"])
        for kind, old, new, ctx, why in report:
            w.writerow([kind, why, old, new, ctx])
    counts = collections.Counter((r[0], r[4]) for r in report)
    for (kind, why), n in sorted(counts.items()):
        print(f"{n:6}  {kind} ({why})")
    print(f"Changes: {len(report)}. Edited files: {', '.join(written)}. Report: {rep}")


if __name__ == "__main__":
    main()
