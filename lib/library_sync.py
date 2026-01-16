import os
import sqlite3
import xbmc
import xbmcaddon
import xbmcvfs
from lib.navidrome_api import NavidromeAPI


class LibrarySync:
    """Handles syncing Navidrome library to Kodi's music database"""
    
    def __init__(self, api, addon):
        """
        Initialize LibrarySync
        
        Args:
            api: NavidromeAPI instance
            addon: xbmcaddon.Addon instance
        """
        self.api = api
        self.addon = addon
    
    def _get_db_path(self):
        """Get path to Kodi's music database"""
        userdata_path = xbmcvfs.translatePath('special://userdata')
        db_path = os.path.join(userdata_path, 'Database', 'MyMusic83.db')
        
        if not os.path.exists(db_path):
            # Try MyMusic82 for Kodi 20
            db_path = os.path.join(userdata_path, 'Database', 'MyMusic82.db')
            
        if not os.path.exists(db_path):
            raise Exception(f"Music database not found at {db_path}")
            
        xbmc.log(f"NAVIDROME SYNC: Using database {db_path}", xbmc.LOGINFO)
        return db_path
    
    def _get_or_create_artist(self, conn, artist_data):
        """Get or create artist in database"""
        cursor = conn.cursor()
        
        # Check if artist exists
        cursor.execute("SELECT idArtist FROM artist WHERE strMusicBrainzArtistID = ?", 
                      (artist_data.get('mbzArtistId', ''),))
        result = cursor.fetchone()
        
        if result:
            return result[0]
        
        # Create new artist
        cursor.execute("""
            INSERT INTO artist (
                strArtist, strMusicBrainzArtistID, strSortName, 
                strGenres, strBiography, dateAdded
            ) VALUES (?, ?, ?, ?, ?, datetime('now'))
        """, (
            artist_data.get('name', ''),
            artist_data.get('mbzArtistId', ''),
            artist_data.get('sortName', artist_data.get('name', '')),
            ', '.join(artist_data.get('genres', [])) if artist_data.get('genres') else '',
            artist_data.get('biography', '')
        ))
        
        return cursor.lastrowid
    
    def _get_or_create_album(self, conn, album_data, artist_kodi_id, path_id):
        """Get or create album in database"""
        cursor = conn.cursor()
        
        # Check if album exists
        mbid = album_data.get('mbzAlbumId', '')
        if mbid:
            cursor.execute("SELECT idAlbum FROM album WHERE strMusicBrainzAlbumID = ?", (mbid,))
            result = cursor.fetchone()
            if result:
                return result[0]
        
        # Extract year from album data
        year = album_data.get('year', 0)
        release_date = str(year) if year else ''
        
        # Create new album
        cursor.execute("""
            INSERT INTO album (
                strAlbum, strMusicBrainzAlbumID, strArtistDisp, strArtistSort,
                strGenres, strReleaseDate, strOrigReleaseDate, bCompilation,
                iDiscTotal, dateAdded, idInfoSetting
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 0)
        """, (
            album_data.get('name', ''),
            mbid,
            album_data.get('artist', ''),
            album_data.get('artistSort', album_data.get('artist', '')),
            album_data.get('genre', ''),
            release_date,
            release_date,
            1 if album_data.get('compilation') else 0,
            album_data.get('discCount', 1)
        ))
        
        album_id = cursor.lastrowid
        
        # Link album to artist
        cursor.execute("""
            INSERT INTO album_artist (idArtist, idAlbum, iOrder, strArtist)
            VALUES (?, ?, 0, ?)
        """, (artist_kodi_id, album_id, album_data.get('artist', '')))
        
        return album_id
    
    def _get_or_create_path(self, conn, path_str):
        """Get or create path in database"""
        cursor = conn.cursor()
        
        cursor.execute("SELECT idPath FROM path WHERE strPath = ?", (path_str,))
        result = cursor.fetchone()
        
        if result:
            return result[0]
        
        cursor.execute("INSERT INTO path (strPath, strHash) VALUES (?, '')", (path_str,))
        return cursor.lastrowid
    
    def _add_song(self, conn, song_data, album_kodi_id, path_id, strm_file):
        """Add song to database"""
        cursor = conn.cursor()
        
        # Extract year from song data
        year = song_data.get('year', 0)
        release_date = str(year) if year else ''
        
        # Calculate track number (disc in upper 4 bytes, track in lower 4 bytes)
        disc_num = song_data.get('discNumber', 1)
        track_num = song_data.get('track', 0)
        itrack = (disc_num << 16) | track_num
        
        cursor.execute("""
            INSERT INTO song (
                idAlbum, idPath, strArtistDisp, strArtistSort, strGenres,
                strTitle, iTrack, iDuration, strReleaseDate, strOrigReleaseDate,
                strFileName, strMusicBrainzTrackID, comment, dateAdded,
                iBitRate, iSampleRate, iChannels
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
        """, (
            album_kodi_id,
            path_id,
            song_data.get('artist', ''),
            song_data.get('artistSort', song_data.get('artist', '')),
            song_data.get('genre', ''),
            song_data.get('title', ''),
            itrack,
            song_data.get('duration', 0),
            release_date,
            release_date,
            os.path.basename(strm_file),
            song_data.get('mbzTrackId', ''),
            song_data.get('comment', ''),
            song_data.get('bitRate', 0),
            song_data.get('sampleRate', 0),
            song_data.get('channels', 2)
        ))
        
        song_id = cursor.lastrowid
        
        # Link song to artist
        artist_name = song_data.get('artist', '')
        if artist_name:
            # Get or create artist
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
    
    def full_sync(self, progress_callback=None):
        """Perform full library sync"""
        try:
            xbmc.log("NAVIDROME SYNC: Starting full sync", xbmc.LOGINFO)
            
            # Get library path
            library_path = self.addon.getSetting('library_path')
            if not library_path:
                raise Exception("Library path not configured")
            
            # Ensure library path exists
            if not xbmcvfs.exists(library_path):
                xbmcvfs.mkdirs(library_path)
            
            # Connect to database
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path)
            
            try:
                # Get all artists
                artists = self.api.get_artists()
                xbmc.log(f"NAVIDROME SYNC: Found {len(artists)} artists", xbmc.LOGINFO)
                
                total_items = len(artists)
                processed = 0
                
                for artist_data in artists:
                    if progress_callback:
                        if hasattr(progress_callback, 'iscanceled') and progress_callback.iscanceled():
                            xbmc.log("NAVIDROME SYNC: Sync cancelled by user", xbmc.LOGINFO)
                            return False
                        
                        progress_callback.update(
                            int((processed / total_items) * 100),
                            f"Syncing {artist_data.get('name', 'Unknown')}"
                        )
                    
                    # Create artist folder
                    artist_name = self._sanitize_filename(artist_data.get('name', 'Unknown'))
                    artist_path = os.path.join(library_path, artist_name)
                    if not xbmcvfs.exists(artist_path):
                        xbmcvfs.mkdirs(artist_path)
                    
                    # Get or create artist in DB
                    artist_kodi_id = self._get_or_create_artist(conn, artist_data)
                    
                    # Get albums for artist
                    albums = self.api.get_artist_albums(artist_data['id'])
                    
                    for album_data in albums:
                        # Create album folder
                        album_name = self._sanitize_filename(album_data.get('name', 'Unknown'))
                        album_path = os.path.join(artist_path, album_name)
                        if not xbmcvfs.exists(album_path):
                            xbmcvfs.mkdirs(album_path)
                        
                        # Get path ID
                        path_id = self._get_or_create_path(conn, album_path + '/')
                        
                        # Get or create album in DB
                        album_kodi_id = self._get_or_create_album(conn, album_data, artist_kodi_id, path_id)
                        
                        # Get songs for album
                        songs = self.api.get_album_songs(album_data['id'])
                        
                        for song_data in songs:
                            # Create .strm file
                            track_num = song_data.get('track', 0)
                            song_title = self._sanitize_filename(song_data.get('title', 'Unknown'))
                            strm_filename = f"{track_num:02d} - {song_title}.strm"
                            strm_path = os.path.join(album_path, strm_filename)
                            
                            # Write stream URL to .strm file
                            stream_url = self.api.get_stream_url(song_data['id'])
                            with open(xbmcvfs.translatePath(strm_path), 'w', encoding='utf-8') as f:
                                f.write(stream_url)
                            
                            # Add song to database
                            self._add_song(conn, song_data, album_kodi_id, path_id, strm_path)
                    
                    processed += 1
                
                conn.commit()
                xbmc.log("NAVIDROME SYNC: Full sync completed successfully", xbmc.LOGINFO)
                return True
                
            finally:
                conn.close()
            
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error during sync: {str(e)}", xbmc.LOGERROR)
            import traceback
            xbmc.log(traceback.format_exc(), xbmc.LOGERROR)
            return False
    
    def incremental_sync(self, progress_callback=None):
        """Perform incremental library sync (stub for now)"""
        xbmc.log("NAVIDROME SYNC: Incremental sync not yet implemented, performing full sync", xbmc.LOGINFO)
        return self.full_sync(progress_callback)
    
    def clear_library(self, progress_callback=None):
        """Clear synced library files"""
        try:
            library_path = self.addon.getSetting('library_path')
            if not library_path or not xbmcvfs.exists(library_path):
                return True
            
            if progress_callback:
                progress_callback.update(0, "Clearing library files...")
            
            # Remove all files and folders
            dirs, files = xbmcvfs.listdir(library_path)
            
            for file in files:
                xbmcvfs.delete(os.path.join(library_path, file))
            
            for dir in dirs:
                self._remove_dir_recursive(os.path.join(library_path, dir))
            
            xbmc.log("NAVIDROME SYNC: Library cleared successfully", xbmc.LOGINFO)
            return True
            
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error clearing library: {str(e)}", xbmc.LOGERROR)
            return False
    
    def _remove_dir_recursive(self, path):
        """Recursively remove directory"""
        dirs, files = xbmcvfs.listdir(path)
        
        for file in files:
            xbmcvfs.delete(os.path.join(path, file))
        
        for dir in dirs:
            self._remove_dir_recursive(os.path.join(path, dir))
        
        xbmcvfs.rmdir(path)
    
    def _sanitize_filename(self, filename):
        """Sanitize filename for filesystem"""
        # Remove invalid characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        return filename