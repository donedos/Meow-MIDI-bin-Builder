import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from midi_bin_builder import (
    MUSICBRAINZ_USER_AGENT,
    create_bin,
    scan_midi_files,
    search_musicbrainz_artists,
)


class MidiBinBuilderTest(unittest.TestCase):
    def test_musicbrainz_search_extracts_artist_candidates(self):
        response_data = {
            "recordings": [
                {
                    "title": "Thank You for Loving Me",
                    "score": "98",
                    "artist-credit": [
                        {
                            "artist": {"name": "Bon Jovi"},
                            "joinphrase": "",
                        }
                    ],
                },
                {
                    "title": "Thank You for Loving Me (Live)",
                    "score": "87",
                    "artist-credit": [
                        {
                            "artist": {"name": "Bon Jovi"},
                            "joinphrase": "",
                        }
                    ],
                },
                {
                    "title": "Thank You for Loving Me",
                    "score": "70",
                    "artist-credit": [
                        {
                            "artist": {"name": "Cover Artist"},
                            "joinphrase": "",
                        }
                    ],
                },
            ]
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(response_data).encode("utf-8")

        with patch("midi_bin_builder.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            matches = search_musicbrainz_artists("Thank You for Loving Me")

        self.assertEqual(
            [
                ("Bon Jovi", "Thank You for Loving Me", 98),
                ("Cover Artist", "Thank You for Loving Me", 70),
            ],
            [
                (match.artist, match.recording_title, match.score)
                for match in matches
            ],
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(MUSICBRAINZ_USER_AGENT, request.get_header("User-agent"))
        self.assertIn("recording", request.full_url)

    def test_musicbrainz_search_rejects_blank_title(self):
        with self.assertRaisesRegex(ValueError, "title cannot be blank"):
            search_musicbrainz_artists("  ")

    def test_scanning_reports_progress_for_supported_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory)
            (source / "one.mid").write_bytes(b"MThd" + b"\x00" * 10)
            (source / "two.kar").write_bytes(b"MThd" + b"\x00" * 10)
            (source / "cover.jpg").write_bytes(b"not midi")
            updates = []

            songs = scan_midi_files(
                source,
                lambda count, path: updates.append((count, path)),
            )

            self.assertEqual(2, len(songs))
            self.assertEqual([1, 2], [count for count, _ in updates])
            self.assertEqual({"one.mid", "two.kar"}, {path for _, path in updates})

    def test_creates_bin_with_song_list_and_mapping(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "midi"
            nested = source / "set one"
            nested.mkdir(parents=True)
            (nested / "1 - Hello.mid").write_bytes(b"MThd" + b"\x00" * 128)
            (source / "2 - Second.KAR").write_bytes(b"MThd" + b"\x01" * 64)
            (source / "cover.jpg").write_bytes(b"not midi")

            songs = scan_midi_files(source)
            self.assertEqual(2, len(songs))
            for song in songs:
                if song.path.name == "1 - Hello.mid":
                    song.title = "Hello, world"
                else:
                    song.title = "Second song"
            output = root / "meow.bin"

            mapping = create_bin(source, output, songs)

            self.assertEqual(root / "meowmap.md", mapping)
            self.assertTrue(zipfile.is_zipfile(output))
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(
                    {
                        "midi/set one/1 - Hello.mid",
                        "midi/2 - Second.KAR",
                        "mapping.md",
                        "songs.txt",
                    },
                    set(archive.namelist()),
                )
                songs_text = archive.read("songs.txt").decode("utf-8")
                self.assertIn("000001\tHello, world\tmidi/set one/1 - Hello.mid", songs_text)
                archive_mapping = archive.read("mapping.md").decode("utf-8")
                self.assertIn(
                    "| 000001 | Hello, world |  |  |  | midi/set one/1 - Hello.mid | 132 |",
                    archive_mapping,
                )

            external_mapping = mapping.read_text(encoding="utf-8")
            self.assertEqual(4, len(external_mapping.splitlines()))
            self.assertIn("| 000002 | Second song |  |  |  | midi/2 - Second.KAR | 68 |", external_mapping)
            self.assertNotIn(".csv", mapping.name)

    def test_extracts_filename_code_and_embedded_midi_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "midi"
            source.mkdir()
            name = b"@tMinamahal Kita@sFreddie Aguilar@gOPM@aMga Awit"
            track = b"\x00\xff\x06" + bytes([len(name)]) + name + b"\x00\xff\x2f\x00"
            midi = (
                b"MThd"
                + b"\x00\x00\x00\x06"
                + b"\x00\x00\x00\x01\x00\x78"
                + b"MTrk"
                + len(track).to_bytes(4, "big")
                + track
            )
            (source / "65 - Minamahal Kita.mid").write_bytes(midi)

            song = scan_midi_files(source)[0]

            self.assertEqual("000065", song.code)
            self.assertEqual("Minamahal Kita", song.title)
            self.assertEqual("Freddie Aguilar", song.artist)
            self.assertEqual("OPM", song.genre)
            self.assertEqual("Mga Awit", song.album)

            mapping = create_bin(source, root / "meow.bin", [song])

            self.assertIn(
                "| 000065 | Minamahal Kita | Freddie Aguilar | OPM | Mga Awit | "
                "midi/65 - Minamahal Kita.mid |",
                mapping.read_text(encoding="utf-8"),
            )

    def test_escapes_markdown_table_cells(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "midi"
            source.mkdir()
            (source / "song.mid").write_bytes(b"midi")
            songs = scan_midi_files(source)
            songs[0].code = "000010"
            songs[0].title = "Title | with\nline break"

            mapping = create_bin(source, root / "meow.bin", songs)

            self.assertIn(
                "| 000010 | Title \\| with<br>line break |  |  |  | midi/song.mid | 4 |",
                mapping.read_text(encoding="utf-8"),
            )

    def test_rejects_duplicate_song_codes_without_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "midi"
            source.mkdir()
            (source / "first.mid").write_bytes(b"first")
            (source / "second.mid").write_bytes(b"second")
            songs = scan_midi_files(source)
            songs[0].code = "100001"
            songs[1].code = "0100001"
            output = root / "meow.bin"

            with self.assertRaisesRegex(ValueError, "must be unique"):
                create_bin(source, output, songs)

            self.assertFalse(output.exists())

    def test_allows_text_song_codes_in_mapping(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "midi"
            source.mkdir()
            (source / "song.mid").write_bytes(b"midi")
            songs = scan_midi_files(source)
            songs[0].code = "KARAOKE-001"

            mapping = create_bin(source, root / "meow.bin", songs)

            self.assertIn(
                "| KARAOKE-001 | song |  |  |  | midi/song.mid | 4 |",
                mapping.read_text(encoding="utf-8"),
            )

    def test_requires_bin_extension(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "midi"
            source.mkdir()
            (source / "song.mid").write_bytes(b"midi")

            with self.assertRaisesRegex(ValueError, "must be meow.bin"):
                create_bin(source, root / "collection.zip", scan_midi_files(source))


if __name__ == "__main__":
    unittest.main()
