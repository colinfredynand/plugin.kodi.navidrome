import json
import urllib.request
import urllib.parse
import hashlib
import random
import string
import xbmc
import xbmcaddon


class NavidromeAPI:
    def __init__(self, server_url, username, password):
        self.server_url = server_url.rstrip('/')
        self.username = username
        self.password = password
        self.client_name = "KodiNavidrome"
        self.api_version = "1.16.1"

        # Get settings
        addon = xbmcaddon.Addon()
        self.enable_transcoding = addon.getSettingBool('enable_transcoding')
        self.max_bitrate = int(addon.getSetting('max_bitrate') or '4')  # Index: 0=64, 1=96, 2=128, 3=160, 4=192, 5=256, 6=320

        # Convert transcode format index to format name
        format_index = int(addon.getSetting('transcode_format') or '0')
        formats = ['mp3', 'opus', 'aac']
        self.transcode_format = formats[format_index]

        self.api_timeout = int(addon.getSetting('api_timeout') or '10')
        self.enable_debug = addon.getSettingBool('enable_debug')
        self.use_native_api = addon.getSettingBool('use_native_api')
        
        # Native API token
        self.native_token = None
        if self.use_native_api:
            self._authenticate_native()
    
    def _authenticate_native(self):
        """Authenticate with Navidrome's native API to get JWT token"""
        try:
            url = f"{self.server_url}/auth/login"
            data = json.dumps({
                'username': self.username,
                'password': self.password
            }).encode('utf-8')
            
            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            
            with urllib.request.urlopen(req, timeout=self.api_timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
                self.native_token = result.get('token')
                if self.native_token:
                    if self.enable_debug:
                        xbmc.log("NAVIDROME API: Native API authenticated", xbmc.LOGINFO)
                return self.native_token is not None
        except Exception as e:
            if self.enable_debug:
                xbmc.log(f"NAVIDROME API: Native auth failed: {str(e)}", xbmc.LOGWARNING)
            self.native_token = None
            return False
    
    def _make_native_request(self, endpoint, params=None):
        """Make a request to Navidrome's native API"""
        if not self.native_token:
            xbmc.log("NAVIDROME API: No native token, falling back to Subsonic", xbmc.LOGWARNING)
            return None
        
        try:
            url = f"{self.server_url}/api/{endpoint}"
            if params:
                url += '?' + urllib.parse.urlencode(params)
            
            req = urllib.request.Request(url)
            req.add_header('x-nd-authorization', f'Bearer {self.native_token}')
            
            with urllib.request.urlopen(req, timeout=10) as response:
                # Update token from response header if present
                new_token = response.headers.get('x-nd-authorization')
                if new_token and new_token.startswith('Bearer '):
                    self.native_token = new_token[7:]
                
                data = json.loads(response.read().decode('utf-8'))
                return data
        except urllib.error.HTTPError as e:
            xbmc.log(f"NAVIDROME NATIVE API ERROR: {e.code} - {e.reason} for {endpoint}", xbmc.LOGERROR)
            return None
        except Exception as e:
            xbmc.log(f"NAVIDROME NATIVE API ERROR: {str(e)}", xbmc.LOGERROR)
            return None
    
    def _generate_token(self):
        """Generate salt and token for Subsonic API authentication"""
        salt = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return salt, token
    
    def _build_url(self, endpoint, params=None):
        """Build Subsonic API URL with authentication"""
        salt, token = self._generate_token()
        
        base_params = {
            'u': self.username,
            't': token,
            's': salt,
            'v': self.api_version,
            'c': self.client_name,
            'f': 'json'
        }
        
        if params:
            base_params.update(params)
        
        query_string = urllib.parse.urlencode(base_params)
        return f"{self.server_url}/rest/{endpoint}?{query_string}"
    
    def _make_request(self, endpoint, params=None):
        """Make a Subsonic API request and return JSON response"""
        try:
            url = self._build_url(endpoint, params)
            if self.enable_debug:
                xbmc.log(f"NAVIDROME API: Requesting {endpoint}", xbmc.LOGINFO)
            
            with urllib.request.urlopen(url, timeout=self.api_timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
                
                # Check for Subsonic API errors
                if 'subsonic-response' in data:
                    subsonic_response = data['subsonic-response']
                    if subsonic_response.get('status') == 'failed':
                        error = subsonic_response.get('error', {})
                        error_msg = error.get('message', 'Unknown error')
                        error_code = error.get('code', 'Unknown')
                        xbmc.log(f"NAVIDROME API ERROR: {error_code} - {error_msg}", xbmc.LOGERROR)
                        return None
                    return subsonic_response
                
                return data
                
        except urllib.error.HTTPError as e:
            xbmc.log(f"NAVIDROME HTTP ERROR: {e.code} - {e.reason} for endpoint {endpoint}", xbmc.LOGERROR)
            return None
        except urllib.error.URLError as e:
            xbmc.log(f"NAVIDROME URL ERROR: {e.reason}", xbmc.LOGERROR)
            return None
        except Exception as e:
            xbmc.log(f"NAVIDROME ERROR: {str(e)}", xbmc.LOGERROR)
            return None
    
    def ping(self):
        """Test connection to server"""
        response = self._make_request('ping')
        return response is not None
    
    def get_artists(self):
        """Get all artists"""
        response = self._make_request('getArtists')
        if response and 'artists' in response:
            indexes = response['artists'].get('index', [])
            all_artists = []
            for index in indexes:
                all_artists.extend(index.get('artist', []))
            return all_artists
        return []
    
    def get_artist(self, artist_id):
        """Get artist details including albums"""
        response = self._make_request('getArtist', {'id': artist_id})
        if response and 'artist' in response:
            return response['artist']
        return None
    
    def get_album(self, album_id):
        """Get album details including tracks"""
        response = self._make_request('getAlbum', {'id': album_id})
        if response and 'album' in response:
            return response['album']
        return None
    
    def get_album_list(self, list_type='alphabeticalByName', size=500, offset=0):
        """
        Get album list
        Types: random, newest, highest, frequent, recent, alphabeticalByName, alphabeticalByArtist
        """
        response = self._make_request('getAlbumList2', {
            'type': list_type,
            'size': size,
            'offset': offset
        })
        if response and 'albumList2' in response:
            return response['albumList2'].get('album', [])
        return []
    
    def get_playlists(self):
        """Get all playlists"""
        response = self._make_request('getPlaylists')
        if response and 'playlists' in response:
            return response['playlists'].get('playlist', [])
        return []
    
    def get_playlist(self, playlist_id):
        """Get playlist details including tracks"""
        response = self._make_request('getPlaylist', {'id': playlist_id})
        if response and 'playlist' in response:
            return response['playlist']
        return None
    
    def search(self, query, artist_count=10, album_count=20, song_count=50):
        """Search for artists, albums, and songs"""
        response = self._make_request('search3', {
            'query': query,
            'artistCount': artist_count,
            'albumCount': album_count,
            'songCount': song_count
        })
        if response and 'searchResult3' in response:
            return response['searchResult3']
        return {}
    
    def get_all_songs(self, size=500, offset=0):
        """Get all songs - try native API first, fall back to Subsonic"""
        # Try native API first
        if self.native_token:
            response = self._make_native_request('song', {
                '_end': offset + size,
                '_start': offset,
                '_sort': 'title',
                '_order': 'ASC'
            })
            if response:
                return response
        
        # Fall back to Subsonic API
        response = self._make_request('getSongsByGenre', {
            'genre': '',  # Empty genre returns all songs
            'count': size,
            'offset': offset
        })
        if response and 'songsByGenre' in response:
            return response['songsByGenre'].get('song', [])
        return []
    
    def get_starred_albums(self):
        """Get starred/favourite albums"""
        response = self._make_request('getStarred2')
        if response and 'starred2' in response:
            return response['starred2'].get('album', [])
        return []
    
    def get_cover_art_url(self, cover_art_id, size=300):
        """Get cover art URL"""
        return self._build_url('getCoverArt', {'id': cover_art_id, 'size': size})
    
    def get_stream_url(self, song_id, max_bit_rate=None):
        """Get stream URL for a song"""
        params = {'id': song_id}
        
        # Use transcoding settings if enabled
        if self.enable_transcoding:
            bitrates = [64, 96, 128, 160, 192, 256, 320]
            params['maxBitRate'] = bitrates[self.max_bitrate]
            params['format'] = self.transcode_format
        elif max_bit_rate:
            params['maxBitRate'] = max_bit_rate
        
        return self._build_url('stream', params)
    
    def update_now_playing(self, track_id):
        """Update now playing status"""
        import time
        response = self._make_request('scrobble', {
            'id': track_id,
            'submission': 'false',
            'time': int(time.time() * 1000)
        })
        return response is not None
    
    def scrobble(self, track_id):
        """Scrobble a track (mark as played)"""
        import time
        response = self._make_request('scrobble', {
            'id': track_id,
            'submission': 'true',
            'time': int(time.time() * 1000)
        })
        return response is not None
    
    def get_internet_radios(self):
        """Get all internet radio stations"""
        response = self._make_request('getInternetRadioStations')
        if response and 'internetRadioStations' in response:
            return response['internetRadioStations'].get('internetRadioStation', [])
        return []
    
    def star(self, item_id, item_type='song'):
        """Star an item (song, album, or artist)"""
        params = {}
        if item_type == 'song':
            params['id'] = item_id
        elif item_type == 'album':
            params['albumId'] = item_id
        elif item_type == 'artist':
            params['artistId'] = item_id
        
        response = self._make_request('star', params)
        return response is not None
    
    def unstar(self, item_id, item_type='song'):
        """Unstar an item (song, album, or artist)"""
        params = {}
        if item_type == 'song':
            params['id'] = item_id
        elif item_type == 'album':
            params['albumId'] = item_id
        elif item_type == 'artist':
            params['artistId'] = item_id
        
        response = self._make_request('unstar', params)
        return response is not None
    
    def set_rating(self, item_id, rating):
        """Set rating for a song (1-5 stars)"""
        response = self._make_request('setRating', {
            'id': item_id,
            'rating': rating
        })
        return response is not None
    
    def create_playlist(self, name, song_ids=None):
        """Create a new playlist"""
        params = {'name': name}
        if song_ids:
            params['songId'] = song_ids
        response = self._make_request('createPlaylist', params)
        return response
    
    def update_playlist(self, playlist_id, song_ids_to_add=None):
        """Add songs to an existing playlist"""
        params = {'playlistId': playlist_id}
        if song_ids_to_add:
            params['songIdToAdd'] = song_ids_to_add
        response = self._make_request('updatePlaylist', params)
        return response is not None
    
    def get_genres(self):
        """Get all genres"""
        response = self._make_request('getGenres')
        if response and 'genres' in response:
            return response['genres'].get('genre', [])
        return []

    def get_songs_by_genre(self, genre, size=500, offset=0):
        """Get songs by genre"""
        response = self._make_request('getSongsByGenre', {
            'genre': genre,
            'count': size,
            'offset': offset
        })
        if response and 'songsByGenre' in response:
            return response['songsByGenre'].get('song', [])
        return []

    def get_albums_by_genre(self, genre, size=500, offset=0):
        """Get albums by genre"""
        response = self._make_request('getAlbumList2', {
            'type': 'byGenre',
            'genre': genre,
            'size': size,
            'offset': offset
        })
        if response and 'albumList2' in response:
            return response['albumList2'].get('album', [])
        return []
