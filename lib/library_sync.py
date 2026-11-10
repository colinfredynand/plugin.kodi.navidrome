"""
Sync the Navidrome library into Kodi's own music library.

Kodi has no API to add streamed music to its library (JSON-RPC only offers
AudioLibrary.Scan, which reads tags from files), so this writes the rows
straight into Kodi's music database, the technique Jellyfin for Kodi uses.

Every song is stored under one path, plugin://plugin.kodi.navidrome/library/,
with "<navidrome id>.<suffix>" as its file name. Kodi joins the two into a
plugin URL, so library playback goes through the addon like any other play,
and the file name is also what identifies our rows on the next sync: no
mapping needs to be stored anywhere. The suffix is required: Kodi opens a
single library song as musicdb://songs/<id>.<ext> and refuses one without an
extension.

The path is its own folder, which the addon lists as empty, because Kodi's
"Update library" re-scans every path in its database: given the addon's root
it walks the whole addon and deletes the songs it cannot read tags from. An
empty folder whose stored hash is '' counts as unchanged and is skipped.

Only rows under that path are touched; music scanned from other sources is
left alone.
"""

import calendar
import os
import re
import sqlite3
import time
from collections import Counter

import xbmc
import xbmcvfs

PLUGIN_PATH = 'plugin://plugin.kodi.navidrome/library/'


def file_name(song_id, suffix):
    return f"{song_id}.{''.join(c for c in (suffix or '') if c.isalnum()) or 'audio'}"


def song_id_from_library_path(path):
    """Navidrome id from the path part of a library song URL, or None."""
    if not path.startswith('/library/'):
        return None
    return path[len('/library/'):].rsplit('.', 1)[0] or None

# Schemas this was written against: 82 = Kodi 19/20, 83 = Kodi 21.
# shortcut: other versions are refused, add them here once checked against
# that Kodi's MyMusicNN.db.
SUPPORTED_SCHEMAS = (82, 83)

_ISO = re.compile(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?$')


class SyncError(Exception):
    pass


def find_music_db():
    """Path of Kodi's live music database (the highest MyMusicNN.db)."""
    folder = xbmcvfs.translatePath('special://database/')
    found = []
    for name in os.listdir(folder):
        match = re.match(r'MyMusic(\d+)\.db$', name)
        if match:
            found.append((int(match.group(1)), os.path.join(folder, name)))
    if not found:
        raise SyncError('Kodi music database not found')
    return max(found)[1]


def kodi_time(iso):
    """Server ISO timestamp -> Kodi's local 'YYYY-MM-DD HH:MM:SS', or None."""
    # Parsed by hand: datetime.strptime breaks inside Kodi's embedded Python.
    match = _ISO.match(iso or '')
    if not match:
        return None
    y, mo, d, h, mi, s = (int(x) for x in match.groups()[:6])
    epoch = calendar.timegm((y, mo, d, h, mi, s))
    zone = match.group(7)
    if zone and zone != 'Z':
        sign = 1 if zone[0] == '+' else -1
        epoch -= sign * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))


def _names(song, key, fallback):
    names = [a.get('name') for a in song.get(key) or [] if a.get('name')]
    return names or ([song[fallback]] if song.get(fallback) else [])


def _genres(song):
    names = [g.get('name') for g in song.get('genres') or [] if g.get('name')]
    return names or ([song['genre']] if song.get('genre') else [])


class _Writer:
    def __init__(self, db, api):
        self.db = db
        self.api = api
        self.artists = {}
        self.genres = {}
        row = db.execute("SELECT idRole FROM role WHERE strRole = 'Artist'").fetchone()
        if row:
            self.role = row[0]
        else:
            self.role = db.execute("INSERT INTO role (strRole) VALUES ('Artist')").lastrowid

    def suffix(self, song):
        # What the stream will really be, so Kodi picks the right decoder.
        if self.api.enable_transcoding:
            return self.api.transcode_format
        return song.get('suffix')

    def path_id(self):
        row = self.db.execute('SELECT idPath FROM path WHERE strPath = ?', (PLUGIN_PATH,)).fetchone()
        if row:
            return row[0]
        return self.db.execute(
            "INSERT INTO path (strPath, strHash) VALUES (?, '')", (PLUGIN_PATH,)).lastrowid

    def set_art(self, media_id, media_type, cover_id):
        self.db.execute("DELETE FROM art WHERE media_id = ? AND media_type = ? AND type = 'thumb'",
                        (media_id, media_type))
        self.db.execute("INSERT INTO art (media_id, media_type, type, url) VALUES (?, ?, 'thumb', ?)",
                        (media_id, media_type, self.api.get_cover_art_url(cover_id, size=600)))

    def artist_id(self, name, navidrome_id=None):
        key = name.lower()
        if key not in self.artists:
            row = self.db.execute(
                'SELECT idArtist FROM artist WHERE strArtist = ? COLLATE NOCASE', (name,)).fetchone()
            if row:
                self.artists[key] = row[0]
            else:
                self.artists[key] = self.db.execute(
                    'INSERT INTO artist (strArtist, strSortName) VALUES (?, ?)',
                    (name, name)).lastrowid
            # Never replace a picture Kodi already has (e.g. from a scraper).
            has_art = self.db.execute(
                "SELECT 1 FROM art WHERE media_id = ? AND media_type = 'artist' AND type = 'thumb'",
                (self.artists[key],)).fetchone()
            if navidrome_id and not has_art:
                self.set_art(self.artists[key], 'artist', navidrome_id)
        return self.artists[key]

    def genre_id(self, name):
        key = name.lower()
        if key not in self.genres:
            row = self.db.execute(
                'SELECT idGenre FROM genre WHERE strGenre = ? COLLATE NOCASE', (name,)).fetchone()
            self.genres[key] = row[0] if row else self.db.execute(
                'INSERT INTO genre (strGenre) VALUES (?)', (name,)).lastrowid
        return self.genres[key]

    def write_album(self, kodi_id, songs):
        first = songs[0]
        artists = [(a.get('name'), a.get('id')) for a in first.get('albumArtists') or []
                   if a.get('name')] or [(first.get('artist') or 'Unknown Artist', first.get('artistId'))]
        genres = []
        for song in songs:
            genres.extend(g for g in _genres(song) if g not in genres)
        values = {
            'strAlbum': first.get('album') or 'Unknown Album',
            'strArtistDisp': first.get('displayAlbumArtist') or ' / '.join(n for n, _ in artists),
            'strGenres': ' / '.join(genres),
            'strReleaseDate': str(first['year']) if first.get('year') else None,
            'strReleaseType': 'album',
            'iDiscTotal': max(int(s.get('discNumber') or 1) for s in songs),
            'iAlbumDuration': sum(int(s.get('duration') or 0) for s in songs),
            'dateAdded': min(filter(None, (kodi_time(s.get('created')) for s in songs)),
                             default=None),
        }
        if kodi_id is None:
            kodi_id = self.db.execute(
                'INSERT INTO album (%s) VALUES (%s)' % (', '.join(values), ', '.join('?' * len(values))),
                list(values.values())).lastrowid
        else:
            self.db.execute('UPDATE album SET %s WHERE idAlbum = ?' % ', '.join(f'{c} = ?' for c in values),
                            list(values.values()) + [kodi_id])

        self.db.execute('DELETE FROM album_artist WHERE idAlbum = ?', (kodi_id,))
        for order, (name, navidrome_id) in enumerate(artists):
            self.db.execute(
                'INSERT OR IGNORE INTO album_artist (idArtist, idAlbum, iOrder, strArtist) VALUES (?, ?, ?, ?)',
                (self.artist_id(name, navidrome_id), kodi_id, order, name))
        self.set_art(kodi_id, 'album', first['albumId'])
        return kodi_id

    def write_song(self, kodi_id, album_id, path_id, song):
        artists = [(a.get('name'), a.get('id')) for a in song.get('artists') or []
                   if a.get('name')] or [(song.get('artist') or 'Unknown Artist', song.get('artistId'))]
        genres = _genres(song)
        values = {
            'idAlbum': album_id,
            'idPath': path_id,
            'strArtistDisp': song.get('displayArtist') or ' / '.join(n for n, _ in artists),
            'strGenres': ' / '.join(genres),
            'strTitle': song.get('title') or 'Unknown Track',
            # Kodi keeps the disc number in the high 16 bits.
            'iTrack': (int(song.get('discNumber') or 1) << 16) | int(song.get('track') or 0),
            'iDuration': int(song.get('duration') or 0),
            'strReleaseDate': str(song['year']) if song.get('year') else None,
            'strFileName': file_name(song['id'], self.suffix(song)),
            # Left empty: Kodi requires (album, track, MusicBrainz id) to be
            # unique, which duplicate files of one recording would violate.
            'strMusicBrainzTrackID': None,
            'iTimesPlayed': int(song.get('playCount') or 0),
            'lastplayed': kodi_time(song.get('played')),
            'userrating': int(song.get('userRating') or 0) * 2,
            'comment': song.get('comment') or '',
            'iBPM': int(song.get('bpm') or 0),
            'iBitRate': int(song.get('bitRate') or 0),
            'iSampleRate': int(song.get('samplingRate') or 0),
            'iChannels': int(song.get('channelCount') or 0),
            'dateAdded': kodi_time(song.get('created')),
        }
        if kodi_id is None:
            kodi_id = self.db.execute(
                'INSERT INTO song (%s) VALUES (%s)' % (', '.join(values), ', '.join('?' * len(values))),
                list(values.values())).lastrowid
        else:
            self.db.execute('UPDATE song SET %s WHERE idSong = ?' % ', '.join(f'{c} = ?' for c in values),
                            list(values.values()) + [kodi_id])
            self.db.execute('DELETE FROM song_artist WHERE idSong = ?', (kodi_id,))
            self.db.execute('DELETE FROM song_genre WHERE idSong = ?', (kodi_id,))

        for order, (name, navidrome_id) in enumerate(artists):
            self.db.execute(
                'INSERT OR IGNORE INTO song_artist (idArtist, idSong, idRole, iOrder, strArtist) '
                'VALUES (?, ?, ?, ?, ?)',
                (self.artist_id(name, navidrome_id), kodi_id, self.role, order, name))
        for order, name in enumerate(genres):
            self.db.execute('INSERT OR IGNORE INTO song_genre (idGenre, idSong, iOrder) VALUES (?, ?, ?)',
                            (self.genre_id(name), kodi_id, order))


def write_library(db_path, songs, api):
    """
    Make the Navidrome part of Kodi's music database equal to ``songs``
    (Subsonic song dicts). An empty list removes it. One transaction: either
    the whole sync lands or nothing changes.

    Returns {'songs': n, 'albums': n, 'removed': n}.
    """
    db = sqlite3.connect(db_path, timeout=120)
    try:
        version = db.execute('SELECT idVersion FROM version').fetchone()[0]
        if version not in SUPPORTED_SCHEMAS:
            raise SyncError(f'Kodi music database version {version} is not supported yet')

        db.execute('BEGIN IMMEDIATE')
        writer = _Writer(db, api)
        path_id = writer.path_id()

        # What we wrote last time, keyed by Navidrome song id.
        existing = {}
        for song_id, album_id, name in db.execute(
                'SELECT idSong, idAlbum, strFileName FROM song WHERE idPath = ?', (path_id,)):
            existing[name.rsplit('.', 1)[0]] = (song_id, album_id)
        old_albums = {album_id for _song, album_id in existing.values()}

        by_album = {}
        for song in songs:
            if song.get('id') and song.get('albumId'):
                by_album.setdefault(song['albumId'], []).append(song)

        # Which Kodi album each Navidrome album already is: the one most of
        # its songs sit in. Each Kodi album is claimed once, so a split album
        # gets a new row instead of two albums sharing one.
        votes = Counter()
        for album, album_songs in by_album.items():
            for song in album_songs:
                if song['id'] in existing:
                    votes[(album, existing[song['id']][1])] += 1
        kodi_album, claimed = {}, set()
        for (album, kodi_id), _count in votes.most_common():
            if album not in kodi_album and kodi_id not in claimed:
                kodi_album[album] = kodi_id
                claimed.add(kodi_id)

        seen = set()
        for album, album_songs in by_album.items():
            album_id = writer.write_album(kodi_album.get(album), album_songs)
            for song in album_songs:
                if song['id'] in seen:
                    continue
                seen.add(song['id'])
                writer.write_song(existing.get(song['id'], (None,))[0], album_id, path_id, song)

        removed = [kodi_id for navidrome_id, (kodi_id, _a) in existing.items()
                   if navidrome_id not in seen]
        db.executemany('DELETE FROM song WHERE idSong = ?', [(i,) for i in removed])
        # Our albums left without songs, then artists left without anything,
        # as Kodi's own library clean-up would do.
        db.executemany(
            'DELETE FROM album WHERE idAlbum = ? AND NOT EXISTS '
            '(SELECT 1 FROM song WHERE song.idAlbum = album.idAlbum)',
            [(i,) for i in old_albums])
        db.execute(
            'DELETE FROM artist WHERE idArtist > 1 '
            'AND idArtist NOT IN (SELECT idArtist FROM song_artist) '
            'AND idArtist NOT IN (SELECT idArtist FROM album_artist)')
        db.commit()
        return {'songs': len(seen), 'albums': len(by_album), 'removed': len(removed)}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def sync(api, progress=None):
    """Fetch the whole library from the server and write it into Kodi."""
    if not api.ping():
        raise SyncError('Navidrome server not reachable')
    songs = api.get_library_songs(progress)
    if songs is None:
        # Never treat a failed download as an empty library.
        raise SyncError('Could not download the song list')
    result = write_library(find_music_db(), songs, api)
    xbmc.log(f"NAVIDROME SYNC: {result}", xbmc.LOGINFO)
    return result


def remove():
    """Take everything this addon added back out of Kodi's library."""
    return write_library(find_music_db(), [], None)
