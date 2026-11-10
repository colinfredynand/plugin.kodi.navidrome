import threading
import time
import urllib.parse

import xbmc
import xbmcaddon

from lib import library_sync, vfs
from lib.navidrome_api import NavidromeAPI, account_settings, account_configured, active_slot

ADDON_ID = 'plugin.kodi.navidrome'
CLIENT_NAME = 'KodiNavidrome'

# How often the playing track is checked. Track changes are detected here, not
# only through player callbacks, because Kodi does not reliably fire them (or
# report a resolvable path) when a playlist advances on its own (issue #8).
POLL_SECONDS = 5

# shortcut: fixed re-sync interval for the Kodi library, make it a setting if
# anyone needs another. A sync also runs at start and after a settings change.
SYNC_INTERVAL = 6 * 3600

# Last.fm rule: a play counts after half the track or 4 minutes, whichever first.
MAX_SCROBBLE_SECONDS = 240


def track_id_from_path(path):
    """
    Navidrome track id from what Kodi reports as playing, or None.

    Kodi reports either the resolved stream URL, or, for items it advanced
    to inside a playlist, the plugin:// URL the item was listed with.
    """
    if not path:
        return None
    # Kodi protocol options (|verifypeer=false) are not part of the query.
    url = urllib.parse.urlparse(path.split('|', 1)[0])
    params = urllib.parse.parse_qs(url.query)

    if url.scheme == 'plugin':
        if url.netloc != ADDON_ID:
            return None
        if params.get('action') == ['play_track']:
            return (params.get('id') or [None])[0]
        # A song in Kodi's Music library.
        return library_sync.song_id_from_library_path(urllib.parse.unquote(url.path))

    # Only our own stream URLs: they carry c=KodiNavidrome.
    if params.get('c') == [CLIENT_NAME]:
        if '/rest/stream' in url.path:
            return (params.get('id') or [None])[0]
        if '/rest/getTranscodeStream' in url.path:
            return (params.get('mediaId') or [None])[0]
    return None


def should_scrobble(position, duration, threshold_percent):
    threshold = duration * threshold_percent / 100 if duration > 0 else MAX_SCROBBLE_SECONDS
    return position >= min(threshold, MAX_SCROBBLE_SECONDS)


class NavidromeMonitor(xbmc.Monitor):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def onSettingsChanged(self):
        xbmc.log("NAVIDROME SERVICE: Settings changed, reinitializing API", xbmc.LOGINFO)
        # Server, account or TLS options may all have moved; drop the cached
        # login so the next request authenticates afresh.
        vfs.clear_session()
        self.service.init_api()


class NavidromePlayer(xbmc.Player):
    """Tracks the playing Navidrome song for now-playing and scrobbling."""

    def __init__(self, service):
        super().__init__()
        self.service = service
        self._reset()

    def _reset(self):
        self.track_id = None
        self.playing_file = None
        self.started_at = None
        self.duration = 0
        self.position = 0
        self.position_at = None   # when position was read; None while paused
        self.scrobbled = False

    def _read_position(self):
        try:
            position = self.getTime()
            self.duration = (self.duration or self.getTotalTime()
                             or self.getMusicInfoTag().getDuration())
        except RuntimeError:
            return False
        if position + 2 < self.position:
            # Time went backwards: a seek back, or Kodi is already on the next
            # gapless track while still naming this file. Judge the listening
            # so far before the old position is lost.
            self._maybe_scrobble()
        self.position = position
        self.position_at = time.time()
        return True

    def _position_now(self):
        # Kodi cannot be asked once playback has stopped, so add the time
        # played since the last reading.
        if self.position_at is None:
            return self.position
        played = self.position + time.time() - self.position_at
        return min(played, self.duration) if self.duration else played

    # Kodi callbacks
    def onAVStarted(self):
        # The same track starting over after real progress (repeat one) is a
        # new play; a track the poll already picked up is left alone.
        if (self.track_id and self._position_now() > POLL_SECONDS * 2
                and self._playing_track_id() == self.track_id):
            self._finish()
        self.poll()

    def onPlayBackStopped(self):
        self._finish()

    def onPlayBackEnded(self):
        self._finish()

    def onPlayBackError(self):
        self._finish()

    def onPlayBackPaused(self):
        self._read_position()
        self.position_at = None
        self._report('paused')

    def onPlayBackResumed(self):
        self._read_position()
        self._report('playing')

    def onPlayBackSeek(self, time_ms, offset_ms):
        paused = self.position_at is None
        self._read_position()
        if paused:
            self.position_at = None
        self._report('paused' if paused else 'playing')

    # Core
    def poll(self):
        """Pick up track changes and scrobble once the threshold is reached."""
        track_id = self._playing_track_id()
        if track_id != self.track_id:
            self._finish()
            if track_id:
                self._start(track_id)
            return

        if not track_id:
            return
        if self.position_at is not None and self._read_position():
            self._maybe_scrobble()

    def _playing_track_id(self):
        try:
            if not self.isPlayingAudio():
                return None
            playing_file = self.getPlayingFile()
        except RuntimeError:
            return None

        # Same file as at start: same track. Kodi resolves the next playlist
        # item early (gapless), overwriting the offline marker mid-track.
        if self.track_id and playing_file == self.playing_file:
            return self.track_id

        track_id = track_id_from_path(playing_file)
        if track_id:
            return track_id

        # Offline copies play from a local special:// file with no id in it;
        # the plugin leaves a marker when it resolves one.
        marker = vfs.get_now_playing(max_age=3600)
        if marker and marker.get('id') and marker.get('path') == playing_file:
            return marker['id']
        return None

    def _start(self, track_id):
        xbmc.log(f"NAVIDROME SERVICE: Started playing track {track_id}", xbmc.LOGINFO)
        self.track_id = track_id
        try:
            self.playing_file = self.getPlayingFile()
        except RuntimeError:
            self.playing_file = None
        self.started_at = time.time()
        self.scrobbled = False
        if not self._read_position():
            self.position_at = time.time()
        self._report('playing')

    def _finish(self):
        if not self.track_id:
            return
        self._maybe_scrobble()
        self._report('stopped')
        self._reset()

    def _maybe_scrobble(self):
        if self.scrobbled or not self.track_id:
            return
        if not self.service.setting_bool('enable_scrobbling'):
            return
        threshold = self.service.setting_int('scrobble_threshold', 50)
        if should_scrobble(self._position_now(), self.duration, threshold):
            xbmc.log(f"NAVIDROME SERVICE: Scrobbling track {self.track_id}", xbmc.LOGINFO)
            self.service.call('scrobble', self.track_id, self.started_at)
            self.scrobbled = True

    def _report(self, state):
        if self.track_id and self.service.setting_bool('enable_now_playing'):
            self.service.call('report_state', self.track_id, state, self._position_now() * 1000)


class NavidromeService:
    def __init__(self):
        self.addon = xbmcaddon.Addon(ADDON_ID)
        self.api = None
        self.monitor = NavidromeMonitor(self)
        self.player = NavidromePlayer(self)
        self.sync_thread = None
        self.next_sync = 0
        xbmc.log("NAVIDROME SERVICE: Starting", xbmc.LOGINFO)
        self.init_api()

    def setting_bool(self, setting_id):
        try:
            return self.addon.getSettingBool(setting_id)
        except Exception:
            return False

    def setting_int(self, setting_id, default):
        try:
            return self.addon.getSettingInt(setting_id) or default
        except Exception:
            return default

    def init_api(self):
        # A fresh Addon object so changed settings are seen.
        self.addon = xbmcaddon.Addon(ADDON_ID)
        # Account or library may have changed: bring Kodi's library in line soon.
        self.next_sync = time.time() + 15
        account = account_settings(self.addon, active_slot(self.addon))
        if not account_configured(account):
            self.api = None
            xbmc.log("NAVIDROME SERVICE: No credentials configured", xbmc.LOGWARNING)
            return
        try:
            self.api = NavidromeAPI(account['server_url'], account['username'],
                                    account['password'], account['music_folder_id'])
            xbmc.log(f"NAVIDROME SERVICE: API initialized for {account['name']}", xbmc.LOGINFO)
        except Exception as e:
            xbmc.log(f"NAVIDROME SERVICE: Error initializing API: {e}", xbmc.LOGERROR)
            self.api = None

    def call(self, method, *args):
        """Call an API method, never letting a network error kill the service."""
        if not self.api:
            return
        try:
            getattr(self.api, method)(*args)
        except Exception as e:
            xbmc.log(f"NAVIDROME SERVICE: {method} failed: {e}", xbmc.LOGERROR)

    def sync_library_if_due(self):
        if not self.api or not self.setting_bool('library_sync'):
            return
        if time.time() < self.next_sync or (self.sync_thread and self.sync_thread.is_alive()):
            return
        self.next_sync = time.time() + SYNC_INTERVAL
        # Own thread: a big library must not hold up scrobbling.
        self.sync_thread = threading.Thread(target=self.call_sync, args=(self.api,), daemon=True)
        self.sync_thread.start()

    @staticmethod
    def call_sync(api):
        try:
            library_sync.sync(api)
        except Exception as e:
            xbmc.log(f"NAVIDROME SERVICE: library sync failed: {e!r}", xbmc.LOGERROR)

    def run(self):
        xbmc.log("NAVIDROME SERVICE: Running", xbmc.LOGINFO)
        while not self.monitor.waitForAbort(POLL_SECONDS):
            self.player.poll()
            self.sync_library_if_due()
        xbmc.log("NAVIDROME SERVICE: Stopped", xbmc.LOGINFO)


if __name__ == '__main__':
    NavidromeService().run()
