"""Library sync against Kodi 21's real music database schema."""
import os
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

for name in ('xbmc', 'xbmcaddon', 'xbmcvfs'):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['xbmc'].log = lambda *a, **k: None
sys.modules['xbmc'].LOGINFO = 0

from lib import library_sync  # noqa: E402

SCHEMA = open(os.path.join(ROOT, 'tests', 'fixtures', 'MyMusic83.sql')).read()


class Api:
    enable_transcoding = False

    def get_cover_art_url(self, cover_id, size=300):
        return f'http://nd/cover/{cover_id}'


def song(song_id, album='al1', title=None, track=1, **extra):
    data = {
        'id': song_id, 'albumId': album, 'album': f'Album {album}', 'title': title or song_id,
        'artist': 'Band', 'artistId': 'ar1', 'track': track, 'discNumber': 1, 'year': 2020,
        'duration': 100, 'suffix': 'flac', 'genres': [{'name': 'Rock'}],
        'artists': [{'id': 'ar1', 'name': 'Band'}],
        'albumArtists': [{'id': 'ar1', 'name': 'Band'}],
        'created': '2026-01-02T03:04:05.123456789+01:00',
    }
    data.update(extra)
    return data


class LibrarySyncTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        db = sqlite3.connect(self.path)
        db.executescript(SCHEMA)
        # Music the user scanned themselves: must never be touched.
        db.executescript("""
            INSERT INTO path (idPath, strPath) VALUES (50, '/music/local/');
            INSERT INTO artist (idArtist, strArtist) VALUES (60, 'Local Artist');
            INSERT INTO album (idAlbum, strAlbum, strMusicBrainzAlbumID) VALUES (70, 'Local Album', 'mbid-1');
            INSERT INTO album_artist VALUES (60, 70, 0, 'Local Artist');
            INSERT INTO song (idSong, idAlbum, idPath, strTitle, strFileName) VALUES (80, 70, 50, 'Local', 'a.flac');
            INSERT INTO song_artist VALUES (60, 80, 1, 0, 'Local Artist');
        """)
        db.commit()
        db.close()

    def tearDown(self):
        os.remove(self.path)

    def q(self, sql, *args):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def sync(self, songs):
        return library_sync.write_library(self.path, songs, Api())

    def ours(self):
        return self.q("SELECT strTitle, strAlbum, strPath || strFileName FROM songview "
                      "WHERE strPath = ? ORDER BY strTitle", library_sync.PLUGIN_PATH)

    def test_first_sync(self):
        result = self.sync([song('s1'), song('s2', track=2), song('s3', album='al2')])
        self.assertEqual(result, {'songs': 3, 'albums': 2, 'removed': 0})
        self.assertEqual(self.ours()[0], (
            's1', 'Album al1', 'plugin://plugin.kodi.navidrome/library/s1.flac'))
        # Two albums without MusicBrainz ids: the June prototype died here
        # on Kodi's UNIQUE index.
        self.assertEqual(self.q("SELECT count(*) FROM album WHERE strMusicBrainzAlbumID IS NULL"), [(2,)])
        self.assertEqual(self.q("SELECT count(*) FROM artist WHERE strArtist = 'Band'"), [(1,)])
        self.assertEqual(self.q("SELECT count(*) FROM art WHERE media_type = 'album'"), [(2,)])
        self.assertEqual(self.q("SELECT strGenre FROM genre"), [('Rock',)])

    def test_resync_keeps_kodi_ids_and_updates(self):
        self.sync([song('s1'), song('s2', track=2)])
        before = self.q("SELECT idSong, idAlbum FROM song WHERE idPath != 50 ORDER BY idSong")
        self.sync([song('s1', title='Renamed', playCount=7, userRating=4,
                        played='2026-03-04T05:06:07Z'), song('s2', track=2)])
        self.assertEqual(before, self.q("SELECT idSong, idAlbum FROM song WHERE idPath != 50 ORDER BY idSong"))
        title, plays, rating, played = self.q(
            "SELECT strTitle, iTimesPlayed, userrating, lastplayed FROM song WHERE strFileName = 's1.flac'")[0]
        self.assertEqual((title, plays, rating), ('Renamed', 7, 8))
        self.assertRegex(played, r'^2026-03-04 \d\d:\d\d:07$')
        self.assertEqual(self.q("SELECT count(*) FROM song_artist"), [(3,)])
        self.assertEqual(self.q("SELECT count(*) FROM album"), [(2,)])

    def test_removed_songs_albums_artists_go(self):
        self.sync([song('s1'), song('s2', album='al2', artists=[{'id': 'ar2', 'name': 'Other'}],
                                    albumArtists=[{'id': 'ar2', 'name': 'Other'}])])
        result = self.sync([song('s1')])
        self.assertEqual(result['removed'], 1)
        self.assertEqual([r[0] for r in self.ours()], ['s1'])
        self.assertEqual(self.q("SELECT strArtist FROM artist WHERE idArtist > 1 ORDER BY 1"),
                         [('Band',), ('Local Artist',)])
        self.assertEqual(self.q("SELECT count(*) FROM album"), [(2,)])

    def test_song_moved_to_new_album(self):
        self.sync([song('s1'), song('s2', track=2)])
        self.sync([song('s1'), song('s2', album='al9', track=2)])
        self.assertEqual({r[1] for r in self.ours()}, {'Album al1', 'Album al9'})

    def test_remove_leaves_local_music_alone(self):
        self.sync([song('s1'), song('s2', track=2)])
        local = [self.q(f"SELECT * FROM {t} WHERE {c}") for t, c in (
            ('song', 'idPath = 50'), ('album', 'idAlbum = 70'), ('artist', 'idArtist = 60'),
            ('album_artist', 'idAlbum = 70'), ('song_artist', 'idSong = 80'))]
        self.assertEqual(self.sync([]), {'songs': 0, 'albums': 0, 'removed': 2})
        self.assertEqual(self.ours(), [])
        self.assertEqual(self.q("SELECT idAlbum FROM album"), [(70,)])
        self.assertEqual(self.q("SELECT idArtist FROM artist ORDER BY 1"), [(1,), (60,)])
        self.assertEqual(self.q("SELECT count(*) FROM art"), [(0,)])
        # dateModified aside, the user's own rows are byte-for-byte the same.
        self.assertEqual(local, [self.q(f"SELECT * FROM {t} WHERE {c}") for t, c in (
            ('song', 'idPath = 50'), ('album', 'idAlbum = 70'), ('artist', 'idArtist = 60'),
            ('album_artist', 'idAlbum = 70'), ('song_artist', 'idSong = 80'))])

    def test_failure_rolls_everything_back(self):
        self.sync([song('s1')])
        bad = song('s2', track=2)
        bad['duration'] = 'not a number'
        with self.assertRaises(ValueError):
            self.sync([song('s9', album='al5'), bad])
        self.assertEqual([r[0] for r in self.ours()], ['s1'])

    def test_duplicate_tracks_and_shared_artist_names(self):
        self.sync([song('s1'), song('s1b'), song('s1'),
                   song('s4', album='al3', artists=[{'id': 'x', 'name': 'local artist'}])])
        self.assertEqual(len(self.ours()), 3)
        self.assertEqual(self.q("SELECT count(*) FROM artist WHERE strArtist = 'Local Artist' COLLATE NOCASE"),
                         [(1,)])

    def test_unknown_schema_refused(self):
        db = sqlite3.connect(self.path)
        db.execute('UPDATE version SET idVersion = 99')
        db.commit()
        db.close()
        with self.assertRaises(library_sync.SyncError):
            self.sync([song('s1')])
        self.assertEqual(self.ours(), [])

    def test_kodi_time(self):
        self.assertIsNone(library_sync.kodi_time(None))
        self.assertIsNone(library_sync.kodi_time('garbage'))
        utc = library_sync.kodi_time('2026-01-02T03:04:05Z')
        self.assertEqual(utc, library_sync.kodi_time('2026-01-02T04:04:05.999999999+01:00'))


if __name__ == '__main__':
    unittest.main()
