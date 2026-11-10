import hashlib
import json
import random
import ssl
import string
import time
import urllib.error
import urllib.parse
import urllib.request

import xbmc
import xbmcaddon

from lib import vfs


# OpenSubsonic extension names
EXT_SONIC_SIMILARITY = 'sonicSimilarity'     # Navidrome v0.62.0 (plugin-needed)
EXT_PLAYBACK_REPORT = 'playbackReport'       # Navidrome v0.62.0
EXT_TOP_SONGS_BY_ID = 'topSongsByArtistId'   # Navidrome v0.64.0

# Subsonic endpoints that accept musicFolderId (Navidrome multi-library).
FOLDER_ENDPOINTS = {
    'getArtists', 'getAlbumList2', 'search3', 'getSongsByGenre',
    'getStarred2', 'getRandomSongs',
}

ACCOUNT_SLOTS = 3

# Both Navidrome's native REST layer and the Subsonic endpoints refuse to
# return more than this many rows in a single response, whatever we ask for.
# Larger pages are assembled client side from several requests.
MAX_REQUEST_SIZE = 500

# How long a cached login (JWT + server issued Subsonic salt/token) is reused
# before we log in again. The JWT rolls on every request, so this only has to
# be shorter than Navidrome's session lifetime.
SESSION_TTL = 6 * 60 * 60

# Bitrates/formats offered by the settings, used to sanitise stored values.
VALID_BITRATES = [64, 96, 128, 160, 192, 256, 320]
VALID_FORMATS = ['mp3', 'opus', 'aac']


def _setting_choice(addon, setting_id, choices, default):
    """
    Read an enum-ish setting tolerantly.

    Old versions of this addon declared these as ``type="enum"``, which stores
    the *index* of the chosen value, while the settings file advertised the
    value itself as the default. Accept either shape so upgrading users don't
    end up transcoding at "4 kbps".
    """
    raw = (addon.getSetting(setting_id) or '').strip()
    if not raw:
        return default
    for choice in choices:
        if raw == str(choice):
            return choice
    try:
        index = int(raw)
    except ValueError:
        return default
    if 0 <= index < len(choices):
        return choices[index]
    return default


def slot_suffix(slot):
    # Slot 1 keeps the original setting ids so existing installs need no migration.
    return '' if slot == 1 else f'_{slot}'


def active_slot(addon):
    try:
        slot = int(addon.getSetting('active_account') or 1)
    except ValueError:
        slot = 1
    return slot if 1 <= slot <= ACCOUNT_SLOTS else 1


def account_settings(addon, slot):
    """Connection settings of one account slot."""
    sfx = slot_suffix(slot)
    server_url = (addon.getSetting(f'server_url{sfx}') or '').strip()
    username = (addon.getSetting(f'username{sfx}') or '').strip()
    return {
        'slot': slot,
        'name': (addon.getSetting(f'account_name{sfx}') or '').strip()
                or (f'{username}@{server_url}' if server_url and username else f'Account {slot}'),
        'server_url': server_url,
        'username': username,
        'password': addon.getSetting(f'password{sfx}') or '',
        'music_folder_id': (addon.getSetting(f'music_folder{sfx}') or '').strip(),
        'music_folder_name': (addon.getSetting(f'music_folder_name{sfx}') or '').strip(),
    }


def account_configured(account):
    return bool(account['server_url'] and account['username'] and account['password'])


class NavidromeAPI:
    def __init__(self, server_url, username, password, music_folder_id=None):
        self.server_url = server_url.rstrip('/')
        self.username = username
        self.password = password
        self.client_name = "KodiNavidrome"
        self.api_version = "1.16.1"
        self.user_agent = "KodiNavidrome/1.0 (+https://kodi.tv)"
        # Library filter; when set, the native API is skipped because only the
        # Subsonic endpoints document musicFolderId.
        self.music_folder_id = str(music_folder_id) if music_folder_id else None

        # Get settings of addon
        addon = xbmcaddon.Addon()
        self.enable_transcoding = addon.getSettingBool('enable_transcoding')
        self.max_bitrate = _setting_choice(addon, 'max_bitrate', VALID_BITRATES, 192)
        self.transcode_format = _setting_choice(addon, 'transcode_format', VALID_FORMATS, 'mp3')

        self.api_timeout = int(addon.getSetting('api_timeout') or '10')
        self.enable_debug = addon.getSettingBool('enable_debug')

        # TLS — self-signed / private CA support
        self.verify_ssl = self._get_bool(addon, 'verify_ssl', True)
        self.ca_cert_path = (addon.getSetting('ca_cert_path') or '').strip()
        self.ssl_context = self._build_ssl_context()

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

        # Reuse the previous login when we still have a fresh one on disk;
        # a plugin process is spawned for every directory listing and a
        # round trip to /auth/login on each of them is pure latency.
        if not self._load_cached_session():
            self._authenticate_native()
            self._detect_opensubsonic()
            self._save_session()

    @staticmethod
    def _get_bool(addon, setting_id, default):
        """getSettingBool but tolerant of the setting not existing yet."""
        try:
            return addon.getSettingBool(setting_id)
        except Exception:
            raw = (addon.getSetting(setting_id) or '').strip().lower()
            if raw in ('true', 'false'):
                return raw == 'true'
            return default

    # TLS
    def _build_ssl_context(self):
        """
        Build the SSL context used for every urllib call.

        With verification disabled the addon talks to servers using a
        self-signed certificate; pointing at a CA bundle instead keeps
        verification on for a private CA, which is the better option.
        """
        if not self.verify_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            xbmc.log(
                "NAVIDROME API: TLS certificate verification disabled by settings",
                xbmc.LOGWARNING
            )
            return context

        if self.ca_cert_path:
            try:
                return ssl.create_default_context(cafile=vfs.translate(self.ca_cert_path))
            except Exception as exc:
                xbmc.log(
                    f"NAVIDROME API: Could not load CA bundle "
                    f"'{self.ca_cert_path}': {exc}. Using system trust store.",
                    xbmc.LOGERROR
                )
        return ssl.create_default_context()

    def _urlopen(self, req):
        """urlopen with the addon's SSL context and timeout applied."""
        return urllib.request.urlopen(req, timeout=self.api_timeout, context=self.ssl_context)

    def kodi_url(self, url):
        """
        Decorate a URL that Kodi itself will fetch (streams, artwork).

        Kodi uses its own cURL stack for these, so the Python SSL context does
        not apply — the trust decision has to travel with the URL as a Kodi
        protocol option instead.
        """
        if not self.verify_ssl and url.lower().startswith('https://'):
            return url + '|verifypeer=false'
        return url

    # Session cache
    def _session_key(self):
        """Identity of the current credentials; a change invalidates the cache."""
        digest = hashlib.sha256(
            f"{self.server_url}\x00{self.username}\x00{self.password}".encode('utf-8')
        ).hexdigest()
        return digest

    def _load_cached_session(self):
        data = vfs.load_session(self._session_key(), SESSION_TTL)
        if not data or not data.get('native_token'):
            return False

        self.native_token = data.get('native_token')
        self.subsonic_salt = data.get('subsonic_salt')
        self.subsonic_token = data.get('subsonic_token')
        self.user_id = data.get('user_id')
        self.is_admin = bool(data.get('is_admin'))
        self.open_subsonic = bool(data.get('open_subsonic'))
        self.os_extensions = set(data.get('extensions') or [])

        if self.enable_debug:
            xbmc.log("NAVIDROME API: Reusing cached session", xbmc.LOGINFO)
        return True

    def _save_session(self):
        if not self.native_token:
            return
        vfs.save_session(self._session_key(), {
            'native_token': self.native_token,
            'subsonic_salt': self.subsonic_salt,
            'subsonic_token': self.subsonic_token,
            'user_id': self.user_id,
            'is_admin': self.is_admin,
            'open_subsonic': self.open_subsonic,
            'extensions': sorted(self.os_extensions),
        })

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

            with self._urlopen(req) as response:
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
        except ssl.SSLError as exc:
            self._log_ssl_error(exc)
            self.native_token = None
            return False
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                self._log_ssl_error(exc.reason)
            elif self.enable_debug:
                xbmc.log(f"NAVIDROME API: Native auth failed: {exc.reason}", xbmc.LOGWARNING)
            self.native_token = None
            return False
        except Exception as e:
            if self.enable_debug:
                xbmc.log(f"NAVIDROME API: Native auth failed: {str(e)}", xbmc.LOGWARNING)
            self.native_token = None
            return False

    def _log_ssl_error(self, exc):
        xbmc.log(
            f"NAVIDROME API: TLS error talking to {self.server_url}: {exc}. "
            "If the server uses a self-signed certificate, either point "
            "'CA certificate' at its CA in the addon settings or turn off "
            "'Verify TLS certificate'.",
            xbmc.LOGERROR
        )

    def _make_native_request(self, endpoint, params=None, _retry=True):
        """
        Make a request to Navidrome's native REST API (/api/...).
        Refreshes the JWT from the response header, captures x-total-count,
        and re-authenticates + retries once on 401.
        Returns parsed JSON (usually a list) or None to signal fallback.
        """
        if not self.native_token or self.music_folder_id:
            return None

        try:
            url = f"{self.server_url}/api/{endpoint}"
            if params:
                url += '?' + urllib.parse.urlencode(params)

            req = urllib.request.Request(url)
            req.add_header('x-nd-authorization', f'Bearer {self.native_token}')
            req.add_header('Accept', 'application/json')
            req.add_header('User-Agent', self.user_agent)

            with self._urlopen(req) as response:
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
                vfs.clear_session()
                if self._authenticate_native():
                    self._save_session()
                    return self._make_native_request(endpoint, params, _retry=False)
            xbmc.log(
                f"NAVIDROME NATIVE API ERROR: {e.code} - {e.reason} for {endpoint}",
                xbmc.LOGERROR
            )
            return None
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                self._log_ssl_error(e.reason)
            else:
                xbmc.log(f"NAVIDROME NATIVE API ERROR: {e.reason}", xbmc.LOGERROR)
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
        if self.music_folder_id and endpoint in FOLDER_ENDPOINTS:
            params = dict(params or {}, musicFolderId=self.music_folder_id)
        try:
            url = self._build_url(endpoint, params)
            if self.enable_debug:
                xbmc.log(f"NAVIDROME API: Requesting {endpoint} {params or ''}", xbmc.LOGINFO)

            req = urllib.request.Request(url)
            req.add_header('User-Agent', self.user_agent)
            req.add_header('Accept', 'application/json')

            with self._urlopen(req) as response:
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
            if isinstance(e.reason, ssl.SSLError):
                self._log_ssl_error(e.reason)
            else:
                xbmc.log(f"NAVIDROME URL ERROR: {e.reason}", xbmc.LOGERROR)
            return None
        except ssl.SSLError as e:
            self._log_ssl_error(e)
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

    # Pagination
    def _paged(self, fetch, size, offset):
        """
        Assemble a page of ``size`` items starting at ``offset``.

        Neither API layer honours a page larger than MAX_REQUEST_SIZE — asking
        for 1000 rows silently yields 500 — so the page is built from as many
        requests as it takes. ``fetch(count, start)`` must return a list, or
        None to signal the request itself failed.

        Returns (items, first_request_failed).
        """
        items = []
        seen = set()
        total = 0
        remaining = max(0, int(size))
        cursor = max(0, int(offset))
        first_failed = False

        while remaining > 0:
            want = min(remaining, MAX_REQUEST_SIZE)
            batch = fetch(want, cursor)

            if batch is None:
                first_failed = not items
                break
            if not batch:
                break

            total = max(total, self.last_total_count)

            fresh = []
            for entry in batch:
                key = entry.get('id') if isinstance(entry, dict) else None
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                fresh.append(entry)

            if not fresh:
                # The server handed back rows we already have, meaning it
                # ignored our offset. Stop rather than loop forever.
                xbmc.log(
                    "NAVIDROME API: server ignored pagination offset "
                    f"{cursor}; stopping at {len(items)} items",
                    xbmc.LOGWARNING
                )
                break

            items.extend(fresh)
            cursor += len(batch)
            remaining -= len(batch)

            if len(batch) < want:
                # Short page — the result set is exhausted.
                break

        self.last_total_count = total
        if self.enable_debug:
            xbmc.log(
                f"NAVIDROME API: page offset={offset} requested={size} "
                f"returned={len(items)} total={total}",
                xbmc.LOGINFO
            )
        return items, first_failed

    # System
    def ping(self):
        """Test connection to server."""
        response = self._make_request('ping')
        return response is not None

    # native-first with Subsonic fallback
    def get_artists(self, size=500, offset=0):
        """Get artists (native first with pagination, Subsonic fallback)."""
        def fetch_native(count, start):
            data = self._make_native_request('artist', {
                '_start': start, '_end': start + count,
                '_sort': 'name', '_order': 'ASC'
            })
            return data if isinstance(data, list) else None

        artists, failed = self._paged(fetch_native, size, offset)
        if artists or not failed:
            return artists

        # Subsonic getArtists has no pagination — slice locally.
        response = self._make_request('getArtists')
        if response and 'artists' in response:
            all_artists = []
            for index in response['artists'].get('index', []):
                all_artists.extend(index.get('artist', []))
            self.last_total_count = len(all_artists)
            return all_artists[offset:offset + size]
        self.last_total_count = 0
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

    def get_song(self, song_id):
        """Get a single track's metadata."""
        response = self._make_request('getSong', {'id': song_id})
        if response and 'song' in response:
            return response['song']
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

            def fetch_native(count, start):
                data = self._make_native_request('album', {
                    '_start': start,
                    '_end': start + count,
                    '_sort': sort,
                    '_order': order
                })
                return data if isinstance(data, list) else None

            albums, failed = self._paged(fetch_native, size, offset)
            if albums or not failed:
                return albums

        # Subsonic fallback (also handles 'random')
        def fetch_subsonic(count, start):
            response = self._make_request('getAlbumList2', {
                'type': list_type,
                'size': count,
                'offset': start
            })
            if response and 'albumList2' in response:
                return response['albumList2'].get('album', [])
            return None

        albums, _failed = self._paged(fetch_subsonic, size, offset)
        return albums

    def get_all_songs(self, size=500, offset=0):
        """Get all songs — native /api/song first, Subsonic search fallback."""
        def fetch_native(count, start):
            data = self._make_native_request('song', {
                '_start': start,
                '_end': start + count,
                '_sort': 'title',
                '_order': 'ASC'
            })
            return data if isinstance(data, list) else None

        songs, failed = self._paged(fetch_native, size, offset)
        if songs or not failed:
            return songs

        # Fallback: an empty search3 query matches the whole library and,
        # unlike getSongsByGenre, paginates properly.
        def fetch_subsonic(count, start):
            response = self._make_request('search3', {
                'query': '',
                'artistCount': 0,
                'albumCount': 0,
                'songCount': count,
                'songOffset': start
            })
            if response and 'searchResult3' in response:
                return response['searchResult3'].get('song', [])
            return None

        songs, _failed = self._paged(fetch_subsonic, size, offset)
        return songs

    def get_library_songs(self, on_progress=None):
        """
        Every song of the selected library in Subsonic format, for the Kodi
        library sync. Returns None if any request failed, so that a network
        error is never mistaken for songs having been deleted.
        """
        songs = []
        while True:
            response = self._make_request('search3', {
                'query': '',
                'artistCount': 0,
                'albumCount': 0,
                'songCount': MAX_REQUEST_SIZE,
                'songOffset': len(songs)
            })
            if not response or 'searchResult3' not in response:
                return None
            batch = response['searchResult3'].get('song', [])
            songs.extend(batch)
            if on_progress:
                on_progress(len(songs))
            if len(batch) < MAX_REQUEST_SIZE:
                return songs

    def get_starred_albums(self, size=500, offset=0):
        """Get starred/favourite albums (native filter first, Subsonic fallback)."""
        def fetch_native(count, start):
            data = self._make_native_request('album', {
                '_start': start, '_end': start + count,
                '_sort': 'starredAt', '_order': 'DESC',
                'starred': 'true'
            })
            return data if isinstance(data, list) else None

        albums, failed = self._paged(fetch_native, size, offset)
        if albums or not failed:
            return albums

        response = self._make_request('getStarred2')
        if response and 'starred2' in response:
            all_albums = response['starred2'].get('album', [])
            self.last_total_count = len(all_albums)
            return all_albums[offset:offset + size]
        self.last_total_count = 0
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
            'albumCount': min(album_count, MAX_REQUEST_SIZE),
            'songCount': min(song_count, MAX_REQUEST_SIZE)
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
        def fetch(count, start):
            response = self._make_request('getSongsByGenre', {
                'genre': genre,
                'count': count,
                'offset': start
            })
            if response and 'songsByGenre' in response:
                return response['songsByGenre'].get('song', [])
            return None

        songs, _failed = self._paged(fetch, size, offset)
        return songs

    def get_albums_by_genre(self, genre, size=500, offset=0):
        """Get albums by genre."""
        def fetch(count, start):
            response = self._make_request('getAlbumList2', {
                'type': 'byGenre',
                'genre': genre,
                'size': count,
                'offset': start
            })
            if response and 'albumList2' in response:
                return response['albumList2'].get('album', [])
            return None

        albums, _failed = self._paged(fetch, size, offset)
        return albums

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
        extension (Navidrome v0.62+, needs a plugin such as AudioMuse-AI).
        Falls back to the metadata-based Instant Mix.
        """
        if self.has_extension(EXT_SONIC_SIMILARITY):
            response = self._make_request('getSonicSimilarTracks', {
                'id': song_id,
                'count': count
            })
            # Spec: top-level sonicMatch array of {entry: Child, similarity}.
            matches = (response or {}).get('sonicMatch') or []
            songs = [m['entry'] for m in matches if isinstance(m, dict) and m.get('entry')]
            if songs:
                return songs
        return self.get_similar_songs(song_id, count)

    def get_top_songs(self, artist_id, artist_name, count=50):
        """Top songs of an artist (Last.fm data); by id on Navidrome v0.64+."""
        params = {'count': count}
        if artist_name:
            params['artist'] = artist_name
        if artist_id and self.has_extension(EXT_TOP_SONGS_BY_ID):
            params['id'] = artist_id
        if 'artist' not in params and 'id' not in params:
            return []
        response = self._make_request('getTopSongs', params)
        if response and 'topSongs' in response:
            return response['topSongs'].get('song', [])
        return []

    # Libraries (Navidrome multi-library)
    def get_music_folders(self):
        response = self._make_request('getMusicFolders')
        if response and 'musicFolders' in response:
            return response['musicFolders'].get('musicFolder', [])
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
        """Get cover art URL (fetched by Kodi)."""
        return self.kodi_url(self._build_url('getCoverArt', {'id': cover_art_id, 'size': size}))

    def get_stream_url(self, song_id, max_bit_rate=None, for_kodi=True):
        """
        Get stream URL for a song.

        ``for_kodi`` decorates the URL with Kodi protocol options; pass False
        when the addon itself will fetch the URL with urllib.
        """
        params = {'id': song_id}
        if self.enable_transcoding:
            params['maxBitRate'] = self.max_bitrate
            params['format'] = self.transcode_format
        elif max_bit_rate:
            params['maxBitRate'] = max_bit_rate
        url = self._build_url('stream', params)
        return self.kodi_url(url) if for_kodi else url

    def open_stream(self, song_id):
        """
        Open the raw audio stream for downloading, honouring the TLS settings.
        Returns the response object (caller must close it) or None.
        """
        try:
            url = self.get_stream_url(song_id, for_kodi=False)
            req = urllib.request.Request(url)
            req.add_header('User-Agent', self.user_agent)
            return self._urlopen(req)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                self._log_ssl_error(exc.reason)
            else:
                xbmc.log(f"NAVIDROME API: stream open failed: {exc.reason}", xbmc.LOGERROR)
            return None
        except Exception as exc:
            xbmc.log(f"NAVIDROME API: stream open failed: {exc}", xbmc.LOGERROR)
            return None

    # Playback reporting / annotation — Subsonic/OpenSubsonic
    def report_state(self, track_id, state, position_ms=0):
        """
        Now-playing state: starting, playing, paused or stopped.

        Uses the OpenSubsonic 'playbackReport' extension (Navidrome v0.62+)
        with ignoreScrobble, so plays are only counted by scrobble(). Older
        servers get a classic submission=false scrobble for start/resume.
        """
        if self.has_extension(EXT_PLAYBACK_REPORT):
            response = self._make_request('reportPlayback', {
                'mediaId': track_id,
                'mediaType': 'song',
                'positionMs': max(0, int(position_ms)),
                'state': state,
                'ignoreScrobble': 'true',
            })
            if response is not None:
                return True
        if state in ('starting', 'playing'):
            return self._make_request('scrobble', {
                'id': track_id, 'submission': 'false'
            }) is not None
        return False

    def scrobble(self, track_id, started_at=None):
        """Register a play; started_at (epoch seconds) is when playback began."""
        response = self._make_request('scrobble', {
            'id': track_id,
            'submission': 'true',
            'time': int((started_at or time.time()) * 1000)
        })
        return response is not None

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
        """
        Set the rating of a song, album or artist (0-5; 0 removes it).

        Subsonic takes the item id directly whatever its type, so the same
        call covers all three.
        """
        try:
            rating = max(0, min(5, int(rating)))
        except (TypeError, ValueError):
            return False
        response = self._make_request('setRating', {
            'id': item_id,
            'rating': rating
        })
        return response is not None

    @staticmethod
    def get_item_rating(item):
        """
        Read the current 0-5 rating off an item from either API shape:
        Subsonic exposes 'userRating', the native API 'rating'.
        """
        for key in ('userRating', 'rating'):
            value = item.get(key)
            if value in (None, ''):
                continue
            try:
                return max(0, min(5, int(value)))
            except (TypeError, ValueError):
                continue
        return 0
