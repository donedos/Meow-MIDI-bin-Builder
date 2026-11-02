# MIDI `.bin` Archive Builder

This desktop utility packages `.mid`, `.midi`, and `.kar` files from a folder
into a ZIP archive named with the `.bin` extension. It uses Python's standard
library only; run it with:

```bash
python3 midi_bin_builder.py
```

Choose a MIDI folder and scan it; the progress bar indicates activity and the
status line reports the number of files found. Double-click a row to edit its
song title and code. A numeric filename prefix supplies the code and is padded
to at least six digits (`65 - Minamahal Kita.mid` becomes `000065`). Files
without a numeric prefix need a code entered before building. Create the archive
to write. The song editor lets you change the title, code, artist/singer, genre,
and album; the list displays the artist/singer. Buttons are blue when available
and gray while scanning or archiving.

- `meow.bin` — a ZIP/Deflate archive containing source files under `midi/`,
  plus `mapping.md` and `songs.txt` at the archive root.
- `meowmap.md` — a sidecar Markdown table mapping each archive path to its song
  code, title, and uncompressed size.

The Markdown mapping includes Song code, Title, Artist, Genre, Album, Archive
path, and Size (bytes). Title, artist, genre, and album are read from embedded
MIDI text metadata when present; missing metadata is left blank. Song codes can
also be text and are preserved for display. Keep both files together when
importing them into MeowsiCat/SongHub; the app assigns internal numeric IDs
where needed for playback controls. The `.bin` extension is only a naming
convention.

For missing artists, open a song and choose **Search MusicBrainz**. The app
sends the song title to MusicBrainz, then asks you to select a matching
recording and artist; the result is only saved when you save the song editor.
An internet connection is required, and title matches may include cover
recordings.

Run the focused tests with:

```bash
python3 -m unittest discover -s tools -p 'test_*.py'
```
