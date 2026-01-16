import xbmc
import xbmcaddon
import xbmcvfs
import sqlite3
import os
import time


class LibrarySync:
    def __init__(self, api, addon):
        self.api = api
        self.addon = addon
        self.addon_id = addon.getAddonInfo('id')
        
        # Get Kodi's music database path
        self.db_path = self._get_music_db_path()
        
        # Track sync state
        self.last_sync_time = self.addon.getSetting('last_sync_time') or '0'
        
    def _get_music_db_path(self):
        """Get the path to Kodi's music database"""
        # Kodi stores databases in userdata/Database/
        userdata_path = xbmcvfs.translatePath('special://userdata/')
        db_folder = os.path.join(userdata_path, 'Database')
        
        # Find the latest MyMusic database (e.g., MyMusic82.db for Kodi 21)
        if os.path.exists(db_folder):
            db_files = [f for f in os.listdir(db_folder) if f.startswith('MyMusic') and f.endswith('.db')]
            if db_files:
                # Sort to get the latest version
                db_files.sort(reverse=True)
                db_path = os.path.join(db_folder, db_files[0])
                xbmc.log(f"NAVIDROME SYNC: Using database {db_path}", xbmc.LOGINFO)
                return db_path
        
        xbmc.log("NAVIDROME SYNC: Could not find music database", xbmc.LOGERROR)
        return None
    
    def _get_db_connection(self):
        """Get a connection to Kodi's music database"""
        if not self.db_path or not os.path.exists(self.db_path):
            return None
        
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Database connection error: {str(e)}", xbmc.LOGERROR)
            return None
    
    def _get_or_create_source_path(self, conn):
        """Get or create a source path for Navidrome content"""
        cursor = conn.cursor()
        
        # Use a virtual path for Navidrome
        source_path = f"plugin://{self.addon_id}/"
        
        # Check if source exists
        cursor.execute("SELECT idPath FROM path WHERE strPath = ?", (source_path,))
        row = cursor.fetchone()
        
        if row:
            return row['idPath']
        
        # Create new path entry
        cursor.execute("INSERT INTO path (strPath) VALUES (?)", (source_path,))
        conn.commit()
        return cursor.lastrowid
    
    def _get_or_create_artist(self, conn, artist_data):
        """Get or create an artist in the database"""
        cursor = conn.cursor()
        
        artist_name = artist_data.get('name', 'Unknown Artist')
        artist_id = artist_data.get('id')
        
        # Check if artist exists (by Navidrome ID stored in strMusicBrainzArtistID)
        cursor.execute(
            "SELECT idArtist FROM artist WHERE strMusicBrainzArtistID = ?",
            (f"navidrome://{artist_id}",)
        )
        row = cursor.fetchone()
        
        if row:
            return row['idArtist']
        
        # Create new artist
        cursor.execute("""
            INSERT INTO artist (strArtist, strMusicBrainzArtistID, strSortName)
            VALUES (?, ?, ?)
        """, (
            artist_name,
            f"navidrome://{artist_id}",
            artist_data.get('sortName', artist_name)
        ))
        conn.commit()
        
        kodi_artist_id = cursor.lastrowid
        xbmc.log(f"NAVIDROME SYNC: Created artist '{artist_name}' (ID: {kodi_artist_id})", xbmc.LOGDEBUG)
        return kodi_artist_id
    
    def _get_or_create_album(self, conn, album_data, artist_kodi_id, path_id):
        """Get or create an album in the database"""
        cursor = conn.cursor()
        
        album_name = album_data.get('name') or album_data.get('album', 'Unknown Album')
        album_id = album_data.get('id')
        
        # Check if album exists
        cursor.execute(
            "SELECT idAlbum FROM album WHERE strMusicBrainzAlbumID = ?",
            (f"navidrome://{album_id}",)
        )
        row = cursor.fetchone()
        
        if row:
            return row['idAlbum']
        
        # Create new album
        year = album_data.get('year', 0)
        
        cursor.execute("""
            INSERT INTO album (
                strAlbum, strMusicBrainzAlbumID, strArtistDisp,
                strGenres, iYear, strReleaseType
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            album_name,
            f"navidrome://{album_id}",
            album_data.get('artist', 'Unknown Artist'),
            album_data.get('genre', ''),
            year,
            'album'
        ))
        conn.commit()
        
        kodi_album_id = cursor.lastrowid
        
        # Link album to artist
        cursor.execute("""
            INSERT INTO album_artist (idArtist, idAlbum, strArtist, iOrder)
            VALUES (?, ?, ?, ?)
        """, (artist_kodi_id, kodi_album_id, album_data.get('artist', 'Unknown Artist'), 0))
        conn.commit()
        
        xbmc.log(f"NAVIDROME SYNC: Created album '{album_name}' (ID: {kodi_album_id})", xbmc.LOGDEBUG)
        return kodi_album_id
    
    def _add_song(self, conn, song_data, album_kodi_id, artist_kodi_id, path_id):
        """Add a song to the database"""
        cursor = conn.cursor()
        
        song_id = song_data.get('id')
        title = song_data.get('title', 'Unknown')
        
        # Check if song already exists
        cursor.execute(
            "SELECT idSong FROM song WHERE strMusicBrainzTrackID = ?",
            (f"navidrome://{song_id}",)
        )
        row = cursor.fetchone()
        
        if row:
            xbmc.log(f"NAVIDROME SYNC: Song '{title}' already exists, skipping", xbmc.LOGDEBUG)
            return row['idSong']
        
        # Create plugin URL for playback
        stream_url = f"plugin://{self.addon_id}/?action=play_track&track_id={song_id}"
        
        # Insert song
        duration = song_data.get('duration', 0)
        track_number = song_data.get('track', 0)
        disc_number = song_data.get('discNumber', 1)
        year = song_data.get('year', 0)
        genre = song_data.get('genre', '')
        
        cursor.execute("""
            INSERT INTO song (
                idAlbum, idPath, strArtistDisp, strGenres, strTitle,
                iTrack, iDuration, iYear, strFileName,
                strMusicBrainzTrackID, iTimesPlayed, lastplayed, rating, userrating
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            album_kodi_id,
            path_id,
            song_data.get('artist', 'Unknown Artist'),
            genre,
            title,
            track_number,
            duration,
            year,
            stream_url,  # Store plugin URL as filename
            f"navidrome://{song_id}",
            0,  # iTimesPlayed
            None,  # lastplayed
            0,  # rating
            0   # userrating
        ))
        conn.commit()
        
        kodi_song_id = cursor.lastrowid
        
        # Link song to artist
        cursor.execute("""
            INSERT INTO song_artist (idArtist, idSong, strArtist, iOrder)
            VALUES (?, ?, ?, ?)
        """, (artist_kodi_id, kodi_song_id, song_data.get('artist', 'Unknown Artist'), 0))
        conn.commit()
        
        xbmc.log(f"NAVIDROME SYNC: Added song '{title}' (ID: {kodi_song_id})", xbmc.LOGDEBUG)
        return kodi_song_id
    
    def full_sync(self, progress_dialog=None):
        """Perform a full library sync"""
        xbmc.log("NAVIDROME SYNC: Starting full sync", xbmc.LOGINFO)
        
        conn = self._get_db_connection()
        if not conn:
            return False
        
        try:
            # Get source path
            path_id = self._get_or_create_source_path(conn)
            
            # Get all artists
            if progress_dialog:
                progress_dialog.update(0, "Fetching artists...")
            
            artists = self.api.get_artists()
            total_artists = len(artists)
            xbmc.log(f"NAVIDROME SYNC: Found {total_artists} artists", xbmc.LOGINFO)
            
            for idx, artist_data in enumerate(artists):
                if progress_dialog and progress_dialog.iscanceled():
                    xbmc.log("NAVIDROME SYNC: Sync cancelled by user", xbmc.LOGINFO)
                    conn.close()
                    return False
                
                artist_name = artist_data.get('name', 'Unknown')
                
                if progress_dialog:
                    progress = int((idx / total_artists) * 100)
                    progress_dialog.update(progress, f"Syncing: {artist_name}")
                
                # Create artist
                artist_kodi_id = self._get_or_create_artist(conn, artist_data)
                
                # Get artist's albums
                artist_details = self.api.get_artist(artist_data['id'])
                if not artist_details:
                    continue
                
                albums = artist_details.get('album', [])
                
                for album_data in albums:
                    # Create album
                    album_kodi_id = self._get_or_create_album(conn, album_data, artist_kodi_id, path_id)
                    
                    # Get album tracks
                    album_details = self.api.get_album(album_data['id'])
                    if not album_details:
                        continue
                    
                    songs = album_details.get('song', [])
                    
                    for song_data in songs:
                        self._add_song(conn, song_data, album_kodi_id, artist_kodi_id, path_id)
            
            # Update last sync time
            self.last_sync_time = str(int(time.time()))
            self.addon.setSetting('last_sync_time', self.last_sync_time)
            
            conn.close()
            
            if progress_dialog:
                progress_dialog.update(100, "Sync complete!")
            
            xbmc.log("NAVIDROME SYNC: Full sync completed successfully", xbmc.LOGINFO)
            return True
            
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error during sync: {str(e)}", xbmc.LOGERROR)
            import traceback
            xbmc.log(traceback.format_exc(), xbmc.LOGERROR)
            conn.close()
            return False
    
    def incremental_sync(self, progress_dialog=None):
        """Perform an incremental sync (only new/changed items)"""
        # For now, just do a full sync
        # TODO: Implement proper incremental sync using timestamps
        xbmc.log("NAVIDROME SYNC: Incremental sync not yet implemented, doing full sync", xbmc.LOGINFO)
        return self.full_sync(progress_dialog)
    
    def clear_library(self, progress_dialog=None):
        """Remove all Navidrome content from Kodi's library"""
        xbmc.log("NAVIDROME SYNC: Clearing library", xbmc.LOGINFO)
        
        conn = self._get_db_connection()
        if not conn:
            return False
        
        try:
            cursor = conn.cursor()
            
            if progress_dialog:
                progress_dialog.update(0, "Clearing library...")
            
            # Find all Navidrome items (identified by navidrome:// prefix in MusicBrainz IDs)
            
            # Delete songs
            cursor.execute("DELETE FROM song WHERE strMusicBrainzTrackID LIKE 'navidrome://%'")
            deleted_songs = cursor.rowcount
            
            # Delete albums
            cursor.execute("DELETE FROM album WHERE strMusicBrainzAlbumID LIKE 'navidrome://%'")
            deleted_albums = cursor.rowcount
            
            # Delete artists
            cursor.execute("DELETE FROM artist WHERE strMusicBrainzArtistID LIKE 'navidrome://%'")
            deleted_artists = cursor.rowcount
            
            # Clean up orphaned entries
            cursor.execute("DELETE FROM album_artist WHERE idAlbum NOT IN (SELECT idAlbum FROM album)")
            cursor.execute("DELETE FROM song_artist WHERE idSong NOT IN (SELECT idSong FROM song)")
            
            conn.commit()
            conn.close()
            
            xbmc.log(f"NAVIDROME SYNC: Cleared {deleted_songs} songs, {deleted_albums} albums, {deleted_artists} artists", xbmc.LOGINFO)
            
            # Reset last sync time
            self.addon.setSetting('last_sync_time', '0')
            
            if progress_dialog:
                progress_dialog.update(100, "Library cleared!")
            
            return True
            
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error clearing library: {str(e)}", xbmc.LOGERROR)
            conn.close()
            return False