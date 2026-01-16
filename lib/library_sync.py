import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
import json
import time
import os
import re


class LibrarySync:
    """
    Handles synchronization between Navidrome and Kodi's music library using .strm files.
    This allows Navidrome content to appear in Kodi's native Artists/Albums/Songs sections.
    """
    
    def __init__(self, api):
        self.api = api
        self.addon = xbmcaddon.Addon()
        self.addon_id = self.addon.getAddonInfo('id')
        
        # Get music library path from settings
        self.library_path = self.addon.getSetting('library_path')
        if not self.library_path:
            # Default to userdata/addon_data/plugin.kodi.navidrome/music/
            self.library_path = xbmcvfs.translatePath(
                f'special://profile/addon_data/{self.addon_id}/music/'
            )
            self.addon.setSetting('library_path', self.library_path)
        
        # Ensure library path exists
        if not xbmcvfs.exists(self.library_path):
            xbmcvfs.mkdirs(self.library_path)
        
        # Sync state file
        self.sync_state_file = xbmcvfs.translatePath(
            f'special://profile/addon_data/{self.addon_id}/sync_state.json'
        )
        self.last_sync_time = self._load_sync_state()
    
    def _load_sync_state(self):
        """Load last sync timestamp"""
        try:
            if xbmcvfs.exists(self.sync_state_file):
                f = xbmcvfs.File(self.sync_state_file, 'r')
                data = json.loads(f.read())
                f.close()
                return data.get('last_sync', 0)
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error loading sync state: {str(e)}", xbmc.LOGWARNING)
        return 0
    
    def _save_sync_state(self):
        """Save last sync timestamp"""
        try:
            f = xbmcvfs.File(self.sync_state_file, 'w')
            f.write(json.dumps({'last_sync': int(time.time())}))
            f.close()
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error saving sync state: {str(e)}", xbmc.LOGERROR)
    
    def _sanitize_filename(self, name):
        """Sanitize filename to remove invalid characters"""
        # Remove invalid characters for filenames
        name = re.sub(r'[<>:"/\\|?*]', '', name)
        # Replace multiple spaces with single space
        name = re.sub(r'\s+', ' ', name)
        # Trim whitespace
        name = name.strip()
        # Limit length
        if len(name) > 200:
            name = name[:200]
        return name if name else 'Unknown'
    
    def _create_nfo_file(self, path, metadata):
        """Create NFO file with metadata"""
        try:
            nfo_path = path.replace('.strm', '.nfo')
            f = xbmcvfs.File(nfo_path, 'w')
            
            # Build NFO XML
            nfo_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            
            if metadata.get('type') == 'artist':
                nfo_content += '<artist>\n'
                nfo_content += f'  <name>{metadata.get("name", "")}</name>\n'
                if metadata.get('mbzId'):
                    nfo_content += f'  <musicbrainzartistid>{metadata["mbzId"]}</musicbrainzartistid>\n'
                nfo_content += '</artist>\n'
            
            elif metadata.get('type') == 'album':
                nfo_content += '<album>\n'
                nfo_content += f'  <title>{metadata.get("title", "")}</title>\n'
                nfo_content += f'  <artist>{metadata.get("artist", "")}</artist>\n'
                if metadata.get('year'):
                    nfo_content += f'  <year>{metadata["year"]}</year>\n'
                if metadata.get('genre'):
                    nfo_content += f'  <genre>{metadata["genre"]}</genre>\n'
                if metadata.get('mbzId'):
                    nfo_content += f'  <musicbrainzalbumid>{metadata["mbzId"]}</musicbrainzalbumid>\n'
                nfo_content += '</album>\n'
            
            elif metadata.get('type') == 'song':
                nfo_content += '<musicvideo>\n'
                nfo_content += f'  <title>{metadata.get("title", "")}</title>\n'
                nfo_content += f'  <artist>{metadata.get("artist", "")}</artist>\n'
                nfo_content += f'  <album>{metadata.get("album", "")}</album>\n'
                if metadata.get('track'):
                    nfo_content += f'  <track>{metadata["track"]}</track>\n'
                if metadata.get('year'):
                    nfo_content += f'  <year>{metadata["year"]}</year>\n'
                if metadata.get('genre'):
                    nfo_content += f'  <genre>{metadata["genre"]}</genre>\n'
                if metadata.get('duration'):
                    nfo_content += f'  <runtime>{metadata["duration"]}</runtime>\n'
                nfo_content += '</musicvideo>\n'
            
            f.write(nfo_content)
            f.close()
            return True
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error creating NFO: {str(e)}", xbmc.LOGERROR)
            return False
    
    def _create_strm_file(self, path, url):
        """Create .strm file with stream URL"""
        try:
            # Ensure directory exists
            dir_path = os.path.dirname(path)
            if not xbmcvfs.exists(dir_path):
                xbmcvfs.mkdirs(dir_path)
            
            # Write .strm file
            f = xbmcvfs.File(path, 'w')
            f.write(url)
            f.close()
            return True
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error creating STRM file: {str(e)}", xbmc.LOGERROR)
            return False
    
    def _download_cover_art(self, cover_url, dest_path):
        """Download cover art to local file"""
        try:
            # Use Kodi's File.Copy for downloading
            success = xbmcvfs.copy(cover_url, dest_path)
            return success
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error downloading cover art: {str(e)}", xbmc.LOGWARNING)
            return False
    
    def sync_full_library(self, progress_callback=None):
        """
        Perform a full library sync using .strm files.
        Creates folder structure: Artist/Album/Track.strm
        """
        xbmc.log("NAVIDROME SYNC: Starting full library sync", xbmc.LOGINFO)
        
        try:
            # Show progress dialog
            if progress_callback is None:
                progress = xbmcgui.DialogProgress()
                progress.create('Navidrome', 'Syncing library...')
            else:
                progress = progress_callback
            
            # Step 1: Get all artists
            progress.update(0, 'Fetching artists...')
            artists = self.api.get_artists()
            total_artists = len(artists)
            
            if total_artists == 0:
                xbmc.log("NAVIDROME SYNC: No artists found", xbmc.LOGWARNING)
                progress.close()
                xbmcgui.Dialog().notification(
                    'Navidrome',
                    'No artists found on server',
                    xbmcgui.NOTIFICATION_WARNING
                )
                return False
            
            xbmc.log(f"NAVIDROME SYNC: Found {total_artists} artists", xbmc.LOGINFO)
            
            # Step 2: Process each artist and their albums
            processed_artists = 0
            total_songs = 0
            
            for artist in artists:
                if progress.iscanceled():
                    xbmc.log("NAVIDROME SYNC: Sync cancelled by user", xbmc.LOGINFO)
                    progress.close()
                    return False
                
                artist_name = artist.get('name', 'Unknown Artist')
                artist_id = artist.get('id')
                
                progress.update(
                    int((processed_artists / total_artists) * 100),
                    f'Processing: {artist_name}',
                    f'{processed_artists + 1} of {total_artists} artists'
                )
                
                # Sanitize artist name for folder
                safe_artist_name = self._sanitize_filename(artist_name)
                artist_path = os.path.join(self.library_path, safe_artist_name)
                
                # Create artist folder
                if not xbmcvfs.exists(artist_path):
                    xbmcvfs.mkdir(artist_path)
                
                # Create artist NFO
                artist_nfo_path = os.path.join(artist_path, 'artist.nfo')
                self._create_nfo_file(artist_nfo_path, {
                    'type': 'artist',
                    'name': artist_name,
                    'mbzId': artist.get('mbzId', '')
                })
                
                # Download artist cover art
                cover_art = artist.get('coverArt')
                if cover_art:
                    cover_url = self.api.get_cover_art_url(cover_art, size=1000)
                    artist_thumb = os.path.join(artist_path, 'folder.jpg')
                    self._download_cover_art(cover_url, artist_thumb)
                
                # Get artist details with albums
                artist_details = self.api.get_artist(artist_id)
                if not artist_details:
                    processed_artists += 1
                    continue
                
                albums = artist_details.get('album', [])
                
                # Process each album
                for album in albums:
                    album_id = album.get('id')
                    album_name = album.get('name', 'Unknown Album')
                    album_year = album.get('year', '')
                    
                    # Get full album details
                    album_details = self.api.get_album(album_id)
                    if not album_details:
                        continue
                    
                    # Sanitize album name for folder
                    safe_album_name = self._sanitize_filename(album_name)
                    if album_year:
                        safe_album_name = f"{album_year} - {safe_album_name}"
                    
                    album_path = os.path.join(artist_path, safe_album_name)
                    
                    # Create album folder
                    if not xbmcvfs.exists(album_path):
                        xbmcvfs.mkdir(album_path)
                    
                    # Create album NFO
                    album_nfo_path = os.path.join(album_path, 'album.nfo')
                    self._create_nfo_file(album_nfo_path, {
                        'type': 'album',
                        'title': album_name,
                        'artist': artist_name,
                        'year': album_year,
                        'genre': album_details.get('genre', ''),
                        'mbzId': album_details.get('mbzId', '')
                    })
                    
                    # Download album cover art
                    album_cover = album_details.get('coverArt')
                    if album_cover:
                        cover_url = self.api.get_cover_art_url(album_cover, size=1000)
                        album_thumb = os.path.join(album_path, 'folder.jpg')
                        self._download_cover_art(cover_url, album_thumb)
                    
                    # Process songs in album
                    songs = album_details.get('song', [])
                    for song in songs:
                        song_id = song.get('id')
                        song_title = song.get('title', 'Unknown Track')
                        track_num = song.get('track', 0)
                        
                        # Sanitize song title
                        safe_song_title = self._sanitize_filename(song_title)
                        
                        # Add track number prefix
                        if track_num:
                            safe_song_title = f"{track_num:02d} - {safe_song_title}"
                        
                        # Get stream URL
                        stream_url = self.api.get_stream_url(song_id)
                        
                        # Create .strm file
                        strm_path = os.path.join(album_path, f"{safe_song_title}.strm")
                        self._create_strm_file(strm_path, stream_url)
                        
                        # Create song NFO
                        self._create_nfo_file(strm_path, {
                            'type': 'song',
                            'title': song_title,
                            'artist': artist_name,
                            'album': album_name,
                            'track': track_num,
                            'year': song.get('year', ''),
                            'genre': song.get('genre', ''),
                            'duration': song.get('duration', 0)
                        })
                        
                        total_songs += 1
                
                processed_artists += 1
            
            # Step 3: Trigger Kodi library scan
            progress.update(95, 'Scanning library...')
            xbmc.executebuiltin(f'UpdateLibrary(music, {self.library_path})')
            
            # Save sync state
            self._save_sync_state()
            
            progress.update(100, 'Sync complete!')
            time.sleep(1)
            progress.close()
            
            xbmc.log(f"NAVIDROME SYNC: Full library sync completed - {processed_artists} artists, {total_songs} songs", xbmc.LOGINFO)
            
            xbmcgui.Dialog().notification(
                'Navidrome',
                f'Synced {processed_artists} artists, {total_songs} songs',
                xbmcgui.NOTIFICATION_INFO,
                5000
            )
            
            return True
            
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error during sync: {str(e)}", xbmc.LOGERROR)
            import traceback
            xbmc.log(f"NAVIDROME SYNC: Traceback: {traceback.format_exc()}", xbmc.LOGERROR)
            
            if progress_callback is None and 'progress' in locals():
                progress.close()
            
            xbmcgui.Dialog().notification(
                'Navidrome',
                'Sync failed - check log',
                xbmcgui.NOTIFICATION_ERROR
            )
            return False
    
    def sync_incremental(self):
        """
        Perform an incremental sync (only new/changed items since last sync).
        """
        xbmc.log("NAVIDROME SYNC: Starting incremental sync", xbmc.LOGINFO)
        
        try:
            # Get recently added albums since last sync
            albums = self.api.get_album_list('newest', size=50)
            
            if not albums:
                xbmc.log("NAVIDROME SYNC: No new albums found", xbmc.LOGINFO)
                return True
            
            new_songs = 0
            
            for album in albums:
                album_id = album.get('id')
                album_name = album.get('name', 'Unknown Album')
                artist_name = album.get('artist', 'Unknown Artist')
                album_year = album.get('year', '')
                
                # Get full album details
                album_details = self.api.get_album(album_id)
                if not album_details:
                    continue
                
                # Build paths
                safe_artist_name = self._sanitize_filename(artist_name)
                safe_album_name = self._sanitize_filename(album_name)
                if album_year:
                    safe_album_name = f"{album_year} - {safe_album_name}"
                
                artist_path = os.path.join(self.library_path, safe_artist_name)
                album_path = os.path.join(artist_path, safe_album_name)
                
                # Skip if album already exists
                if xbmcvfs.exists(album_path):
                    continue
                
                # Create folders
                if not xbmcvfs.exists(artist_path):
                    xbmcvfs.mkdir(artist_path)
                if not xbmcvfs.exists(album_path):
                    xbmcvfs.mkdir(album_path)
                
                # Create album NFO and cover
                album_nfo_path = os.path.join(album_path, 'album.nfo')
                self._create_nfo_file(album_nfo_path, {
                    'type': 'album',
                    'title': album_name,
                    'artist': artist_name,
                    'year': album_year,
                    'genre': album_details.get('genre', ''),
                    'mbzId': album_details.get('mbzId', '')
                })
                
                album_cover = album_details.get('coverArt')
                if album_cover:
                    cover_url = self.api.get_cover_art_url(album_cover, size=1000)
                    album_thumb = os.path.join(album_path, 'folder.jpg')
                    self._download_cover_art(cover_url, album_thumb)
                
                # Process songs
                songs = album_details.get('song', [])
                for song in songs:
                    song_id = song.get('id')
                    song_title = song.get('title', 'Unknown Track')
                    track_num = song.get('track', 0)
                    
                    safe_song_title = self._sanitize_filename(song_title)
                    if track_num:
                        safe_song_title = f"{track_num:02d} - {safe_song_title}"
                    
                    stream_url = self.api.get_stream_url(song_id)
                    strm_path = os.path.join(album_path, f"{safe_song_title}.strm")
                    self._create_strm_file(strm_path, stream_url)
                    
                    new_songs += 1
            
            if new_songs > 0:
                # Trigger library scan
                xbmc.executebuiltin(f'UpdateLibrary(music, {self.library_path})')
                
                xbmc.log(f"NAVIDROME SYNC: Incremental sync completed - {new_songs} new songs", xbmc.LOGINFO)
                
                xbmcgui.Dialog().notification(
                    'Navidrome',
                    f'Added {new_songs} new songs',
                    xbmcgui.NOTIFICATION_INFO,
                    3000
                )
            
            # Save sync state
            self._save_sync_state()
            return True
            
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error during incremental sync: {str(e)}", xbmc.LOGERROR)
            return False
    
    def clear_library(self):
        """Remove all Navidrome content from Kodi's library"""
        xbmc.log("NAVIDROME SYNC: Clearing library", xbmc.LOGINFO)
        
        try:
            # Confirm with user
            dialog = xbmcgui.Dialog()
            if not dialog.yesno('Navidrome', 'Remove all synced music from library?'):
                return False
            
            # Remove all files in library path
            if xbmcvfs.exists(self.library_path):
                dirs, files = xbmcvfs.listdir(self.library_path)
                
                # Remove all artist folders
                for artist_dir in dirs:
                    artist_path = os.path.join(self.library_path, artist_dir)
                    xbmcvfs.rmdir(artist_path, force=True)
            
            # Clean the library
            xbmc.executebuiltin('CleanLibrary(music)')
            
            # Remove sync state
            if xbmcvfs.exists(self.sync_state_file):
                xbmcvfs.delete(self.sync_state_file)
            
            xbmcgui.Dialog().notification(
                'Navidrome',
                'Library cleared',
                xbmcgui.NOTIFICATION_INFO
            )
            
            xbmc.log("NAVIDROME SYNC: Library cleared successfully", xbmc.LOGINFO)
            return True
            
        except Exception as e:
            xbmc.log(f"NAVIDROME SYNC: Error clearing library: {str(e)}", xbmc.LOGERROR)
            xbmcgui.Dialog().notification(
                'Navidrome',
                'Error clearing library',
                xbmcgui.NOTIFICATION_ERROR
            )
            return False
    
    def get_sync_status(self):
        """Get current sync status"""
        if self.last_sync_time == 0:
            return "Never synced"
        
        import datetime
        last_sync = datetime.datetime.fromtimestamp(self.last_sync_time)
        return f"Last synced: {last_sync.strftime('%Y-%m-%d %H:%M:%S')}"