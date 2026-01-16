# ============================================================================
# lib/library_sync.py - Direct database sync like Jellyfin (NO PATH NEEDED)
# ============================================================================

import sqlite3
import time
import xbmc
import xbmcaddon
import xbmcvfs
import os

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')

class LibrarySync:
    def __init__(self, api):
        self.api = api
        self.lock_file = os.path.join(xbmcvfs.translatePath('special://temp/'), 
                                      f'{ADDON_ID}.sync.lock')

    def _acquire_lock(self):
        """Acquire sync lock"""
        if xbmcvfs.exists(self.lock_file):
            stat = xbmcvfs.Stat(self.lock_file)
            lock_age = time.time() - stat.st_mtime()
            if lock_age < 3600:  # 1 hour
                xbmc.log("NAVIDROME SYNC: Another sync is running", xbmc.LOGWARNING)
                return False
            xbmcvfs.delete(self.lock_file)

        lock = xbmcvfs.File(self.lock_file, 'w')
        lock.write(str(time.time()))
        lock.close()
        return True

    def _release_lock(self):
        """Release sync lock"""
        if xbmcvfs.exists(self.lock_file):
            xbmcvfs.delete(self.lock_file)

    def _get_kodi_db_path(self):
        """Get path to Kodi's music database"""
        db_dir = xbmcvfs.translatePath('special://database/')
        db_files = [f for f in os.listdir(db_dir) if f.startswith('MyMusic') and f.endswith('.db')]

        if not db_files:
            raise Exception("Could not find Kodi music database")

        db_files.sort(reverse=True)
        latest_db = os.path.join(db_dir, db_files[0])

        xbmc.log(f"NAVIDROME SYNC: Using database {latest_db}", xbmc.LOGINFO)
        return latest_db

    def _get_or_create_path(self, conn):
        """Get or create a single path entry for all Navidrome content"""
        cursor = conn.cursor()

        # Use plugin:// URL as the path (like Jellyfin does)
        plugin_path = f"plugin://{ADDON_ID}/"

        cursor.execute("SELECT idPath FROM path WHERE strPath = ?", (plugin_path,))
        result = cursor.fetchone()

        if result:
            return result[0]

        cursor.execute("INSERT INTO path (strPath, strHash) VALUES (?, '')", (plugin_path,))
        return cursor.lastrowid

    def _get_or_create_artist(self, conn, artist_data):
        """Get or create artist in Kodi database"""
        cursor = conn.cursor()

        # Use Navidrome ID as unique identifier
        navidrome_id = f"navidrome://{artist_data['id']}"

        cursor.execute("""
            SELECT idArtist FROM artist 
            WHERE strMusicBrainzArtistID = ?
        """, (navidrome_id,))

        result = cursor.fetchone()
        if result:
            return result[0]

        # Create new artist
        cursor.execute("""
            INSERT INTO artist (
                strArtist, strMusicBrainzArtistID, strSortName,
                strGenres, strBiography, dateAdded
            )
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        """, (
            artist_data.get('name', 'Unknown Artist'),
            navidrome_id,
            artist_data.get('sortName', artist_data.get('name', '')),
            ', '.join(artist_data.get('genres', [])),
            artist_data.get('biography', '')
        ))

        return cursor.lastrowid

    def _get_or_create_album(self, conn, album_data, artist_kodi_id):
        """Get or create album in Kodi database"""
        cursor = conn.cursor()

        # Use Navidrome ID as unique identifier
        navidrome_id = f"navidrome://{album_data['id']}"

        cursor.execute("""
            SELECT idAlbum FROM album 
            WHERE strMusicBrainzAlbumID = ?
        """, (navidrome_id,))

        result = cursor.fetchone()
        if result:
            return result[0]

        # Create new album
        year = album_data.get('year', 0)
        cursor.execute("""
            INSERT INTO album (
                strAlbum, strMusicBrainzAlbumID, strArtistDisp,
                strGenres, strReleaseDate, iYear, dateAdded, idInfoSetting
            )
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), 0)
        """, (
            album_data.get('name', 'Unknown Album'),
            navidrome_id,
            album_data.get('artist', 'Unknown Artist'),
            album_data.get('genre', ''),
            str(year) if year else '',
            year
        ))

        album_kodi_id = cursor.lastrowid

        # Link album to artist
        cursor.execute("""
            INSERT OR IGNORE INTO album_artist (idArtist, idAlbum, iOrder, strArtist)
            VALUES (?, ?, 0, ?)
        """, (artist_kodi_id, album_kodi_id, album_data.get('artist', 'Unknown Artist')))

        return album_kodi_id

    def _add_song(self, conn, song_data, album_kodi_id, path_id):
        """Add song to Kodi database"""
        cursor = conn.cursor()

        # Use Navidrome ID as unique identifier
        navidrome_id = f"navidrome://{song_data['id']}"

        # Check if song already exists
        cursor.execute("""
            SELECT idSong FROM song 
            WHERE strMusicBrainzTrackID = ?
        """, (navidrome_id,))

        result = cursor.fetchone()
        if result:
            return result[0]

        # Create plugin URL as filename (like Jellyfin does)
        plugin_url = f"plugin://{ADDON_ID}/?action=play_track&id={song_data['id']}"

        # Calculate track number (disc in upper 16 bits, track in lower 16 bits)
        disc_num = song_data.get('discNumber', 1)
        track_num = song_data.get('track', 0)
        itrack = (disc_num << 16) | track_num

        year = song_data.get('year', 0)

        cursor.execute("""
            INSERT INTO song (
                idAlbum, idPath, strArtistDisp, strGenres, strTitle,
                iTrack, iDuration, iYear, strFileName,
                strMusicBrainzTrackID, dateAdded,
                iBitRate, iSampleRate, iChannels
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
        """, (
            album_kodi_id,
            path_id,
            song_data.get('artist', 'Unknown Artist'),
            song_data.get('genre', ''),
            song_data.get('title', 'Unknown'),
            itrack,
            song_data.get('duration', 0),
            year,
            plugin_url,  # Use plugin URL as filename
            navidrome_id,
            song_data.get('bitRate', 0),
            song_data.get('sampleRate', 0),
            2  # Default to stereo
        ))

        song_id = cursor.lastrowid

        # Link song to artist
        artist_name = song_data.get('artist', 'Unknown Artist')

        # Get or create artist for song
        cursor.execute("SELECT idArtist FROM artist WHERE strArtist = ?", (artist_name,))
        result = cursor.fetchone()

        if result:
            artist_id = result[0]
        else:
            cursor.execute("""
                INSERT INTO artist (strArtist, dateAdded)
                VALUES (?, datetime('now'))
            """, (artist_name,))
            artist_id = cursor.lastrowid

        # Get role ID for "Artist" (usually 1)
        cursor.execute("SELECT idRole FROM role WHERE strRole = 'Artist'")
        result = cursor.fetchone()
        role_id = result[0] if result else 1

        cursor.execute("""
            INSERT INTO song_artist (idArtist, idSong, idRole, iOrder, strArtist)
            VALUES (?, ?, ?, 0, ?)
        """, (artist_id, song_id, role_id, artist_name))

        return song_id

    def full_sync(self):
        """Perform full library sync"""
        if not self._acquire_lock():
            xbmc.log("NAVIDROME SYNC: Sync already in progress", xbmc.LOGWARNING)
            return False

        try:
            xbmc.log("NAVIDROME SYNC: Starting full sync", xbmc.LOGINFO)

            # Get Kodi database path
            db_path = self._get_kodi_db_path()

            # Connect to database with WAL mode for better concurrency
            conn = sqlite3.connect(db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")

            try:
                # Get or create single path entry for all content
                path_id = self._get_or_create_path(conn)

                # Get all artists from Navidrome
                artists = self.api.get_artists()
                xbmc.log(f"NAVIDROME SYNC: Found {len(artists)} artists", xbmc.LOGINFO)

                total_tracks = 0

                for i, artist_data in enumerate(artists):
                    # Progress update every 10 artists
                    if i % 10 == 0:
                        xbmc.log(f"NAVIDROME SYNC: Processing artist {i+1}/{len(artists)}", xbmc.LOGINFO)

                    # Get or create artist
                    artist_kodi_id = self._get_or_create_artist(conn, artist_data)

                    # Get albums for artist
                    albums = self.api.get_artist_albums(artist_data['id'])

                    for album_data in albums:
                        # Get or create album
                        album_kodi_id = self._get_or_create_album(conn, album_data, artist_kodi_id)

                        # Get tracks for album
                        tracks = self.api.get_album_tracks(album_data['id'])

                        for track_data in tracks:
                            # Add track to database
                            self._add_song(conn, track_data, album_kodi_id, path_id)
                            total_tracks += 1

                # Commit all changes
                conn.commit()
                xbmc.log(f"NAVIDROME SYNC: Full sync completed ({total_tracks} tracks)", xbmc.LOGINFO)

                # Update library timestamp
                ADDON.setSetting('last_sync', str(int(time.time())))

                # Trigger Kodi library update
                xbmc.executebuiltin('UpdateLibrary(music)')

                return True

            finally:
                conn.close()

        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error during sync: {e}", xbmc.LOGERROR)
            import traceback
            xbmc.log(traceback.format_exc(), xbmc.LOGERROR)
            return False
        finally:
            self._release_lock()

    def incremental_sync(self):
        """Perform incremental sync"""
        xbmc.log("NAVIDROME SYNC: Incremental sync not implemented, doing full sync", xbmc.LOGINFO)
        return self.full_sync()

    def clear_library(self):
        """Clear all Navidrome items from Kodi library"""
        if not self._acquire_lock():
            xbmc.log("NAVIDROME SYNC: Sync in progress, cannot clear", xbmc.LOGWARNING)
            return False

        try:
            xbmc.log("NAVIDROME SYNC: Clearing library", xbmc.LOGINFO)

            # Get Kodi database path
            db_path = self._get_kodi_db_path()

            # Connect to database
            conn = sqlite3.connect(db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")

            try:
                cursor = conn.cursor()

                # Delete songs with navidrome:// MusicBrainz IDs
                cursor.execute("""
                    DELETE FROM song 
                    WHERE strMusicBrainzTrackID LIKE 'navidrome://%'
                """)

                # Delete albums with navidrome:// MusicBrainz IDs
                cursor.execute("""
                    DELETE FROM album 
                    WHERE strMusicBrainzAlbumID LIKE 'navidrome://%'
                """)

                # Delete artists with navidrome:// MusicBrainz IDs
                cursor.execute("""
                    DELETE FROM artist 
                    WHERE strMusicBrainzArtistID LIKE 'navidrome://%'
                """)

                # Delete plugin path
                cursor.execute("""
                    DELETE FROM path 
                    WHERE strPath LIKE 'plugin://plugin.kodi.navidrome/%'
                """)

                conn.commit()

                xbmc.log("NAVIDROME SYNC: Library cleared successfully", xbmc.LOGINFO)

                # Clear sync timestamp
                ADDON.setSetting('last_sync', '')

                # Trigger Kodi library clean
                xbmc.executebuiltin('CleanLibrary(music)')

                return True

            finally:
                conn.close()

        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error clearing library: {e}", xbmc.LOGERROR)
            import traceback
            xbmc.log(traceback.format_exc(), xbmc.LOGERROR)
            return False
        finally:
            self._release_lock()