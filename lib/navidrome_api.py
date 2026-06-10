import json
import time
import urllib.request
import urllib.parse
import urllib.error
import hashlib
import random
import string
import xbmc
import xbmcaddon


# OpenSubsonic extension names
EXT_SONIC_SIMILARITY = 'sonicSimilarity'   # Navidrome v0.62.0 (plugin-needed)
EXT_PLAYBACK_REPORT = 'playbackReport'     # Navidrome v0.62.0
EXT_SONG_LYRICS = 'songLyrics'
EXT_TRANSCODE_OFFSET = 'transcodeOffset'


class NavidromeAPI:
    def __init__(self, server_url, username, password):
        self.server_url = server_url.rstrip('/')
        self.username = username
        self.password = password
        self.client_name = "KodiNavidrome"
        self.api_version = "1.16.1"
        self.user_agent = "KodiNavidrome/1.0 (+https://kodi.tv)"

        # Get settings of addon
        addon = xbmcaddon.Addon()
        self.enable_transcoding = addon.getSettingBool('enable_transcoding')
        self.max_bitrate = int(addon.getSetting('max_bitrate') or '192')

        self.transcode_format = addon.getSetting('transcode_format') or 'mp3'

        self.api_timeout = int(addon.getSetting('api_timeout') or '10')
        self.enable_debug = addon.getSettingBool('enable_debug')

        # Native API (JWT) auth parameters
        self.native_token = None          # x-nd-authorization bearer token
        self.subsonic_salt = None         # server-issued salt (from /auth/login)
        self.subsonic_token = None        # server-issued token (md5(password+salt))
        self.user_id = None
        self.is_admin = False

        # Pagination total
        self.last_total_count = 0

        # OpenSubsonic capability detection
        self.open_subsonic = False
        self.os_extensions = set()

        # Authenticate native first; capability-detect the Subsonic layer
        self._authenticate_native()
        self._detect_opensubsonic()

    # Native api
    def _authenticate_native(self):
        """Authenticate with Navidrome's native API to obtain JWT + Subsonic creds."""
        try:
            url = f"{self.server_url}/auth/login"
            data = json.dumps({
                'username': self.username,
                'password': self.password
            }).encode('utf-8')

            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': self.user_agent
                },
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=self.api_timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
                self.native_token = result.get('token')
                # Navidrome returns server-issued Subsonic auth so we don't
                # have to compute our own salt/token for /rest endpoints.
                self.subsonic_salt = result.get('subsonicSalt')
                self.subsonic_token = result.get('subsonicToken')
                self.user_id = result.get('id')
                self.is_admin = bool(result.get('isAdmin', False))

                if self.enable_debug:
                    xbmc.log(
                        "NAVIDROME API: Native auth OK "
                        f"(token={'yes' if self.native_token else 'no'}, "
                        f"subsonic={'yes' if self.subsonic_token else 'no'})",
                        xbmc.LOGINFO
                    )
                return self.native_token is not None
        except Exception as e:
            if self.enable_debug:
                xbmc.log(f"NAVIDROME API: Native auth failed: {str(e)}", xbmc.LOGWARNING)
            self.native_token = None
            return False

    def _make_native_request(self, endpoint, params=None, _retry=True):
        """
        Make a request to Navidrome's native REST API (/api/...).
        Refreshes the JWT from the response header, captures x-total-count,
        and re-authenticates + retries once on 401.
        Returns parsed JSON (usually a list) or None to signal fallback.
        """
        if not self.native_token:
            return None

        try:
            url = f"{self.server_url}/api/{endpoint}"
            if params:
                url += '?' + urllib.parse.urlencode(params)

            req = urllib.request.Request(url)
            req.add_header('x-nd-authorization', f'Bearer {self.native_token}')
            req.add_header('Accept', 'application/json')
            req.add_header('User-Agent', self.user_agent)

            with urllib.request.urlopen(req, timeout=self.api_timeout) as response:
                # Refresh rolling token if the server issued a new one
                new_token = response.headers.get('x-nd-authorization')
                if new_token and new_token.startswith('Bearer '):
                    self.native_token = new_token[7:]

                # Capture total count for pagination
                total = response.headers.get('x-total-count')
                self.last_total_count = int(total) if total and total.isdigit() else 0

                body = response.read().decode('utf-8')
                return json.loads(body) if body else None

        except urllib.error.HTTPError as e:
            if e.code == 401 and _retry:
                if self.enable_debug:
                    xbmc.log("NAVIDROME NATIVE API: 401, re-authenticating", xbmc.LOGINFO)
                if self._authenticate_native():
                    return self._make_native_request(endpoint, params, _retry=False)
            xbmc.log(
                f"NAVIDROME NATIVE API ERROR: {e.code} - {e.reason} for {endpoint}",
                xbmc.LOGERROR
            )
            return None
        except Exception as e:
            xbmc.log(f"NAVIDROME NATIVE API ERROR: {str(e)}", xbmc.LOGERROR)
            return None

    # Subsonic/ Opensubsonic (rest/...)
    def _generate_token(self):
        """Generate salt + token for Subsonic auth (fallback when no server creds)."""
        salt = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return salt, token

    def _build_url(self, endpoint, params=None):
        """Build a Subsonic API URL. Prefers server-issued salt/token from login."""
        if self.subsonic_salt and self.subsonic_token:
            salt, token = self.subsonic_salt, self.subsonic_token
        else:
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

        query_string = urllib.parse.urlencode(base_params, doseq=True)
        return f"{self.server_url}/rest/{endpoint}?{query_string}"

    def _make_request(self, endpoint, params=None):
        """Make a Subsonic API request and return the subsonic-response payload."""
        try:
            url = self._build_url(endpoint, params)
            if self.enable_debug:
                xbmc.log(f"NAVIDROME API: Requesting {endpoint}", xbmc.LOGINFO)

            req = urllib.request.Request(url)
            req.add_header('User-Agent', self.user_agent)
            req.add_header('Accept', 'application/json')

            with urllib.request.urlopen(req, timeout=self.api_timeout) as response:
                data = json.loads(response.read().decode('utf-8'))

                if 'subsonic-response' in data:
                    subsonic_response = data['subsonic-response']
                    if subsonic_response.get('status') == 'failed':
                        error = subsonic_response.get('error', {})
                        error_msg = error.get('message', 'Unknown error')
                        error_code = error.get('code', 'Unknown')
                        xbmc.log(
                            f"NAVIDROME API ERROR: {error_code} - {error_msg}",
                            xbmc.LOGERROR
                        )
                        return None
                    return subsonic_response

                return data

        except urllib.error.HTTPError as e:
            xbmc.log(
                f"NAVIDROME HTTP ERROR: {e.code} - {e.reason} for endpoint {endpoint}",
                xbmc.LOGERROR
            )
            return None
        except urllib.error.URLError as e:
            xbmc.log(f"NAVIDROME URL ERROR: {e.reason}", xbmc.LOGERROR)
            return None
        except Exception as e:
            xbmc.log(f"NAVIDROME ERROR: {str(e)}", xbmc.LOGERROR)
            return None

    def _detect_opensubsonic(self):
        """Probe getOpenSubsonicExtensions once and cache supported extensions."""
        response = self._make_request('getOpenSubsonicExtensions')
        if response:
            self.open_subsonic = True
            exts = response.get('openSubsonicExtensions', [])
            for ext in exts:
                name = ext.get('name') if isinstance(ext, dict) else ext
                if name:
                    self.os_extensions.add(name)
            if self.enable_debug:
                xbmc.log(
                    f"NAVIDROME API: OpenSubsonic extensions: {sorted(self.os_extensions)}",
                    xbmc.LOGINFO
                )
        else:
            self.open_subsonic = False

    def has_extension(self, name):
        """Return True if the server advertises a given OpenSubsonic extension."""
        return name in self.os_extensions

    # System
    def ping(self):
        """Test connection to server."""
        response = self._make_request('ping')
        return response is not None

    # native-first with Subsonic fallback
    def get_artists(self):
        """Get all artists (native first, Subsonic fallback)."""
        data = self._make_native_request('artist', {
            '_start': 0, '_end': 0, '_sort': 'name', '_order': 'ASC'
        })
        if isinstance(data, list):
            return data

        response = self._make_request('getArtists')
        if response and 'artists' in response:
            all_artists = []
            for index in response['artists'].get('index', []):
                all_artists.extend(index.get('artist', []))
            return all_artists
        return []

    def get_artist(self, artist_id):
        """Get artist details including albums (Subsonic for the nested album list)."""
        response = self._make_request('getArtist', {'id': artist_id})
        if response and 'artist' in response:
            return response['artist']
        return None

    def get_album(self, album_id):
        """Get album details including tracks (Subsonic for the nested song list)."""
        response = self._make_request('getAlbum', {'id': album_id})
        if response and 'album' in response:
            return response['album']
        return None

    def get_album_list(self, list_type='alphabeticalByName', size=500, offset=0):
        """
        Get album list. Native first when the sort maps cleanly, else Subsonic.
        Types: random, newest, highest, frequent, recent,
               alphabeticalByName, alphabeticalByArtist
        """
        native_sort = {
            'alphabeticalByName': ('name', 'ASC'),
            'alphabeticalByArtist': ('artist', 'ASC'),
            'newest': ('createdAt', 'DESC'),
            'recent': ('playDate', 'DESC'),
            'frequent': ('playCount', 'DESC'),
            'highest': ('rating', 'DESC'),
            'starred': ('starredAt', 'DESC'),
        }

        if list_type in native_sort:
            sort, order = native_sort[list_type]
            data = self._make_native_request('album', {
                '_start': offset,
                '_end': offset + size,
                '_sort': sort,
                '_order': order
            })
            if isinstance(data, list):
                return data

        # Subsonic fallback (also handles 'random')
        response = self._make_request('getAlbumList2', {
            'type': list_type,
            'size': size,
            'offset': offset
        })
        if response and 'albumList2' in response:
            return response['albumList2'].get('album', [])
        return []

    def get_all_songs(self, size=500, offset=0):
        """Get all songs — native /api/song first, Subsonic genre-hack fallback."""
        data = self._make_native_request('song', {
            '_start': offset,
            '_end': offset + size,
            '_sort': 'title',
            '_order': 'ASC'
        })
        if isinstance(data, list):
            return data

        # Fallback: empty-genre trick returns all songs on Subsonic
        response = self._make_request('getSongsByGenre', {
            'genre': '',
            'count': size,
            'offset': offset
        })
        if response and 'songsByGenre' in response:
            return response['songsByGenre'].get('song', [])
        return []

    def get_starred_albums(self):
        """Get starred/favourite albums (native filter first, Subsonic fallback)."""
        data = self._make_native_request('album', {
            '_start': 0, '_end': 0,
            '_sort': 'starredAt', '_order': 'DESC',
            'starred': 'true'
        })
        if isinstance(data, list):
            return data

        response = self._make_request('getStarred2')
        if response and 'starred2' in response:
            return response['starred2'].get('album', [])
        return []

    # Playlists
    def get_playlists(self):
        """Get all playlists."""
        response = self._make_request('getPlaylists')
        if response and 'playlists' in response:
            return response['playlists'].get('playlist', [])
        return []

    def get_playlist(self, playlist_id):
        """Get playlist details including tracks."""
        response = self._make_request('getPlaylist', {'id': playlist_id})
        if response and 'playlist' in response:
            return response['playlist']
        return None

    def create_playlist(self, name, song_ids=None):
        """Create a new playlist."""
        params = {'name': name}
        if song_ids:
            params['songId'] = song_ids
        return self._make_request('createPlaylist', params)

    def update_playlist(self, playlist_id, song_ids_to_add=None):
        """Add songs to an existing playlist."""
        params = {'playlistId': playlist_id}
        if song_ids_to_add:
            params['songIdToAdd'] = song_ids_to_add
        response = self._make_request('updatePlaylist', params)
        return response is not None

    # Search
    def search(self, query, artist_count=10, album_count=20, song_count=50):
        """Search for artists, albums, and songs."""
        response = self._make_request('search3', {
            'query': query,
            'artistCount': artist_count,
            'albumCount': album_count,
            'songCount': song_count
        })
        if response and 'searchResult3' in response:
            return response['searchResult3']
        return {}

    # Genres
    def get_genres(self):
        """Get all genres."""
        response = self._make_request('getGenres')
        if response and 'genres' in response:
            return response['genres'].get('genre', [])
        return []

    def get_songs_by_genre(self, genre, size=500, offset=0):
        """Get songs by genre."""
        response = self._make_request('getSongsByGenre', {
            'genre': genre,
            'count': size,
            'offset': offset
        })
        if response and 'songsByGenre' in response:
            return response['songsByGenre'].get('song', [])
        return []

    def get_albums_by_genre(self, genre, size=500, offset=0):
        """Get albums by genre."""
        response = self._make_request('getAlbumList2', {
            'type': 'byGenre',
            'genre': genre,
            'size': size,
            'offset': offset
        })
        if response and 'albumList2' in response:
            return response['albumList2'].get('album', [])
        return []

    # Similarity — Instant Mix (v0.60) + sonicSimilarity (v0.62, plugin needed)
    def get_similar_songs(self, song_id, count=50):
        """
        Instant Mix: similar songs for a track (Navidrome v0.60+).
        Uses getSimilarSongs2 (ID3) and falls back to getSimilarSongs.
        """
        response = self._make_request('getSimilarSongs2', {'id': song_id, 'count': count})
        if response and 'similarSongs2' in response:
            return response['similarSongs2'].get('song', [])

        response = self._make_request('getSimilarSongs', {'id': song_id, 'count': count})
        if response and 'similarSongs' in response:
            return response['similarSongs'].get('song', [])
        return []

    def get_sonic_similar_tracks(self, song_id, count=50):
        """
        Audio-based similar tracks via the OpenSubsonic 'sonicSimilarity'
        extension (Navidrome v0.62.0). Requires a plugin (e.g. AudioMuse-AI)
        that provides the capability; otherwise returns []. Falls back to the
        metadata-based Instant Mix when the extension is unavailable.

        NOTE: endpoint params are best-effort pending official spec; adjust
        if the server rejects them.
        """
        if not self.has_extension(EXT_SONIC_SIMILARITY):
            return self.get_similar_songs(song_id, count)

        response = self._make_request('getSonicSimilarTracks', {
            'id': song_id,
            'count': count
        })
        if response:
            # Tolerate a few likely container shapes
            for key in ('sonicSimilarTracks', 'similarSongs2', 'similarSongs'):
                container = response.get(key)
                if isinstance(container, dict):
                    songs = container.get('song') or container.get('track')
                    if songs is not None:
                        return songs
            if isinstance(response.get('song'), list):
                return response['song']
        # Graceful degradation
        return self.get_similar_songs(song_id, count)

    def find_sonic_path(self, from_id, to_id, count=25):
        """
        Build a 'sonic path' between two tracks via the 'sonicSimilarity'
        extension (Navidrome v0.62.0). Returns [] if the extension is absent.

        NOTE: param names are best-effort pending official spec.
        """
        if not self.has_extension(EXT_SONIC_SIMILARITY):
            return []

        response = self._make_request('findSonicPath', {
            'from': from_id,
            'to': to_id,
            'count': count
        })
        if response:
            for key in ('sonicPath', 'similarSongs2', 'similarSongs'):
                container = response.get(key)
                if isinstance(container, dict):
                    songs = container.get('song') or container.get('track')
                    if songs is not None:
                        return songs
            if isinstance(response.get('song'), list):
                return response['song']
        return []

    # Radios
    def get_internet_radios(self):
        """Get all internet radio stations."""
        response = self._make_request('getInternetRadioStations')
        if response and 'internetRadioStations' in response:
            return response['internetRadioStations'].get('internetRadioStation', [])
        return []

    # Media
    def get_cover_art_url(self, cover_art_id, size=300):
        """Get cover art URL."""
        return self._build_url('getCoverArt', {'id': cover_art_id, 'size': size})

    def get_stream_url(self, song_id, max_bit_rate=None):
        """Get stream URL for a song."""
        params = {'id': song_id}
        if self.enable_transcoding:
            params['maxBitRate'] = self.max_bitrate
            params['format'] = self.transcode_format
        elif max_bit_rate:
            params['maxBitRate'] = max_bit_rate
        return self._build_url('stream', params)

    # Playback reporting / annotation — Subsonic/OpenSubsonic
    def report_playback(self, track_id, submission=True):
        """
        Report playback. Prefers the OpenSubsonic 'playbackReport' extension
        (Navidrome v0.62.0) and falls back to classic scrobble otherwise.
        submission=False => 'now playing'; submission=True => played.
        """
        now_ms = int(time.time() * 1000)
        if self.has_extension(EXT_PLAYBACK_REPORT):
            response = self._make_request('reportPlayback', {
                'id': track_id,
                'submission': 'true' if submission else 'false',
                'time': now_ms
            })
            if response is not None:
                return True
            # fall through to scrobble on failure

        response = self._make_request('scrobble', {
            'id': track_id,
            'submission': 'true' if submission else 'false',
            'time': now_ms
        })
        return response is not None

    def update_now_playing(self, track_id):
        """Update now playing status (kept for API compatibility)."""
        return self.report_playback(track_id, submission=False)

    def scrobble(self, track_id):
        """Scrobble a track / mark as played (kept for API compatibility)."""
        return self.report_playback(track_id, submission=True)

    def star(self, item_id, item_type='song'):
        """Star an item (song, album, or artist)."""
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
        """Unstar an item (song, album, or artist)."""
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
        """Set rating for a song (1-5 stars)."""
        response = self._make_request('setRating', {
            'id': item_id,
            'rating': rating
        })
        return response is not None