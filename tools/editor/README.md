# Polyhymnia editor

This tool edits the export files from `jfa export-names`. It writes the names in the style of the library. It uses sentence case for titles and the band style for artists. It also corrects file-name rests, errors in the character set and typos.

The tool gave the edited files of the library (1,053 albums). A test with the 5 original exports gives the same 5 edited files, byte for byte.

## Files

| File | Contents |
|---|---|
| `edit_names.py` | The program. It uses only the Python standard library (Python 3.9 or newer). |
| `rules.py` | All rules: protected words, protected phrases, band names, hand-made corrections and the 934 artist names of the library |

## Use

1. Put the export file in a folder. Example: `new_albums_20261126-0900.json`.
2. Start the tool:

   ```sh
   python3 edit_names.py new_albums_20261126-0900.json
   ```

   Result: the tool writes `new_albums_20261126-0900_edited.json` and `changes_for_review.csv` next to the export file.

3. Read `changes_for_review.csv`. Each row shows one change: what, why, before, after and where.
4. Examine each title change. Look for names, places and titles of works that became lowercase.
5. If a name became lowercase, add it to `rules.py`:
   - For one word, add it to `KEEP2`. Example: `Moroder`.
   - For more than one word, add it to `PHRASES`. Example: `Wild West End`.
   - For a new band, add the official name and the new form to `BAND_MAP`. Example: `"Massive Attack": "Massive attack"`.
6. Do step 2 again. Then do steps 3 to 5 again, until all changes are correct.
7. Correct the last special conditions manually in the edited file. Do not change the `id` values.

To edit more than one file, give all files. Example:

```sh
python3 edit_names.py jellyfin_names_part0*.json -o edited/
```

## What the tool does

1. It changes titles in Title Case ("Every Word Capitalized") to sentence case. It does not change titles that are already in sentence case.
2. It protects the words and phrases in `rules.py`, the artist names of the library, and names after "feat.".
3. It keeps acronyms, Roman numerals, musical keys and words with an internal capital.
4. It writes descriptors in brackets in lowercase, for example "(live)" and "(remastered)".
5. It removes file-name rests, for example "01. " and "-easymp3s".
6. It corrects errors in the character set and stray spaces.
7. It changes artist names with the band rules in `BAND_MAP`.

## Limitations

1. The tool knows only the names in `rules.py` and in the artist list. A new name in a title, for example a city, can become lowercase. Step 4 finds these names.
2. Titles in all capitals do not change, because many are official stylizations. Examine them manually.
3. German nouns become lowercase. This is the style of the library.
4. The tool does not add Romanian diacritics. Add them manually, for example from the official track list.

The full rules for the style are in section 6 of the handover document `polyhymnia-handover.md`.
