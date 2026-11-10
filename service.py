import time
import urllib.parse

import xbmc
import xbmcaddon

from lib import vfs
from lib.navidrome_api import NavidromeAPI, account_settings, account_configured, active_slot

ADDON_ID = 'plugin.kodi.navidrome'
CLIENT_NAME = 'KodiNavidrome'

# How often the playing track is checked. Track changes are detected here, not
# only through player callbacks, because Kodi does not reliably fire them (or
# report a resolvable path) when a playlist advances on its own (issue #8).
POLL_SECONDS = 5

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
        if url.netloc == ADDON_ID and params.get('action') == ['play_track']:
            return (params.get('id') or [None])[0]
        return None

    # Only our own stream URLs: they carry c=KodiNavidrome.
    if '/rest/stream' in url.path and params.get('c') == [CLIENT_NAME]:
        return (params.get('id') or [None])[0]
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
        self.scrobbled = False

    # Kodi callbacks
    def onAVStarted(self):
        # The same track starting over after real progress (repeat one) is a
        # new play; a track the poll already picked up is left alone.
        if (self.track_id and self.position > POLL_SECONDS * 2
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
        self._report('paused')

    def onPlayBackResumed(self):
        self._report('playing')

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
        try:
            self.position = self.getTime()
            if not self.duration:
                self.duration = self.getTotalTime()
        except RuntimeError:
            return
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
        try:
            self.position = self.getTime()
            self.duration = self.getTotalTime()
        except RuntimeError:
            self.position, self.duration = 0, 0
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
        if should_scrobble(self.position, self.duration, threshold):
            xbmc.log(f"NAVIDROME SERVICE: Scrobbling track {self.track_id}", xbmc.LOGINFO)
            self.service.call('scrobble', self.track_id, self.started_at)
            self.scrobbled = True

    def _report(self, state):
        if self.track_id and self.service.setting_bool('enable_now_playing'):
            self.service.call('report_state', self.track_id, state, self.position * 1000)


class NavidromeService:
    def __init__(self):
        self.addon = xbmcaddon.Addon(ADDON_ID)
        self.api = None
        self.monitor = NavidromeMonitor(self)
        self.player = NavidromePlayer(self)
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

    def run(self):
        xbmc.log("NAVIDROME SERVICE: Running", xbmc.LOGINFO)
        while not self.monitor.waitForAbort(POLL_SECONDS):
            self.player.poll()
        xbmc.log("NAVIDROME SERVICE: Stopped", xbmc.LOGINFO)


if __name__ == '__main__':
    NavidromeService().run()
