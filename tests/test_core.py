"""Unit tests for the Kodi-independent logic. Run: python3 -m unittest discover tests"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Minimal stand-ins for Kodi's modules.
xbmc = types.ModuleType('xbmc')
xbmc.LOGDEBUG = xbmc.LOGINFO = xbmc.LOGWARNING = xbmc.LOGERROR = 0
xbmc.log = lambda *a, **k: None
xbmc.Monitor = type('Monitor', (), {'__init__': lambda self: None})
xbmc.Player = type('Player', (), {'__init__': lambda self: None})
sys.modules['xbmc'] = xbmc
for name in ('xbmcaddon', 'xbmcvfs'):
    sys.modules[name] = types.ModuleType(name)

from lib import navidrome_api  # noqa: E402
from lib.navidrome_api import NavidromeAPI  # noqa: E402
import service  # noqa: E402


class FakeAddon:
    def __init__(self, **values):
        self.values = values

    def getSetting(self, key):
        return self.values.get(key, '')


class TrackIdTest(unittest.TestCase):
    def test_plugin_url_from_playlist_advance(self):
        path = 'plugin://plugin.kodi.navidrome/?action=play_track&id=abc123'
        self.assertEqual(service.track_id_from_path(path), 'abc123')

    def test_plugin_url_of_a_kodi_library_song(self):
        path = 'plugin://plugin.kodi.navidrome/library/abc123.flac'
        self.assertEqual(service.track_id_from_path(path), 'abc123')

    def test_stream_url_with_kodi_options(self):
        path = ('https://nd.example/rest/stream?u=x&id=s1&c=KodiNavidrome&f=json'
                '|verifypeer=false')
        self.assertEqual(service.track_id_from_path(path), 's1')

    def test_foreign_urls_ignored(self):
        self.assertIsNone(service.track_id_from_path('plugin://plugin.other/?action=play_track&id=1'))
        self.assertIsNone(service.track_id_from_path('https://x/rest/stream?id=1&c=OtherClient'))
        self.assertIsNone(service.track_id_from_path(''))
        self.assertIsNone(service.track_id_from_path('plugin://plugin.kodi.navidrome/library/'))
        self.assertIsNone(service.track_id_from_path('plugin://plugin.kodi.navidrome/?action=songs'))


class FakeService:
    def __init__(self):
        self.calls = []

    def setting_bool(self, _):
        return True

    def setting_int(self, _, default):
        return default

    def call(self, method, *args):
        self.calls.append((method, args[0]))


class FakePlayer(service.NavidromePlayer):
    """Player whose 'Kodi state' is set by the test."""
    file = None
    time = 0.0
    total = 200.0

    def isPlayingAudio(self):
        return self.file is not None

    def getPlayingFile(self):
        return self.file

    def getTime(self):
        return self.time

    def getTotalTime(self):
        return self.total


def plugin_url(track_id):
    return f'plugin://plugin.kodi.navidrome/?action=play_track&id={track_id}'


class PlaylistScrobbleTest(unittest.TestCase):
    """Issue #8: every track of an auto-advancing playlist is scrobbled."""

    def test_auto_advance_without_callbacks(self):
        svc = FakeService()
        player = FakePlayer(svc)
        for track in ('a', 'b', 'c'):
            player.file, player.time = plugin_url(track), 0.0
            player.poll()
            player.time = 150.0  # past 50% of 200s
            player.poll()
        player.file = None
        player.onPlayBackEnded()
        scrobbled = [t for m, t in svc.calls if m == 'scrobble']
        self.assertEqual(scrobbled, ['a', 'b', 'c'])

    def test_offline_track_survives_marker_overwrite(self):
        markers = {'value': {'id': 'a', 'path': 'special://media/a.flac'}}
        service.vfs.get_now_playing = lambda max_age=None: markers['value']
        svc = FakeService()
        player = FakePlayer(svc)
        player.file, player.time = 'special://media/a.flac', 0.0
        player.poll()
        # Gapless: Kodi resolves the next item while 'a' still plays.
        markers['value'] = {'id': 'b', 'path': 'special://media/b.flac'}
        player.time = 150.0
        player.poll()
        self.assertEqual(player.track_id, 'a')
        self.assertIn(('scrobble', 'a'), svc.calls)

    def test_stop_between_polls_counts_time_since_last_poll(self):
        svc = FakeService()
        player = FakePlayer(svc)
        player.file, player.time = plugin_url('a'), 95.0   # threshold is 100s
        player.poll()
        player.position_at -= 6                           # 6s of playback pass
        player.onPlayBackStopped()
        self.assertIn(('scrobble', 'a'), svc.calls)

    def test_gapless_change_reports_new_time_before_new_file(self):
        svc = FakeService()
        player = FakePlayer(svc)
        player.file, player.time, player.total = plugin_url('a'), 5.0, 12.0
        player.poll()
        player.position_at -= 4          # 9s in, past half of 12s
        player.time = 0.1                # Kodi already counts the next track
        player.onAVStarted()
        self.assertIn(('scrobble', 'a'), svc.calls)

    def test_paused_time_is_not_listening(self):
        svc = FakeService()
        player = FakePlayer(svc)
        player.file, player.time = plugin_url('a'), 95.0
        player.poll()
        player.onPlayBackPaused()
        player.onPlayBackStopped()
        self.assertNotIn(('scrobble', 'a'), svc.calls)

    def test_skipped_track_not_scrobbled(self):
        svc = FakeService()
        player = FakePlayer(svc)
        player.file, player.time = plugin_url('a'), 10.0
        player.poll()
        player.file = plugin_url('b')
        player.poll()
        self.assertNotIn(('scrobble', 'a'), svc.calls)
        self.assertIn(('report_state', 'b'), svc.calls)

    def test_threshold_caps_at_four_minutes(self):
        self.assertTrue(service.should_scrobble(240, 3600, 50))
        self.assertFalse(service.should_scrobble(239, 3600, 50))
        self.assertTrue(service.should_scrobble(100, 200, 50))


def bare_api(**attrs):
    api = NavidromeAPI.__new__(NavidromeAPI)
    api.music_folder_id = None
    api.os_extensions = set()
    api.native_token = 'jwt'
    api.__dict__.update(attrs)
    return api


class ApiTest(unittest.TestCase):
    def test_library_filter_skips_native_api(self):
        api = bare_api(music_folder_id='2')
        self.assertIsNone(api._make_native_request('album'))

    def test_library_filter_added_to_supported_endpoints(self):
        api = bare_api(music_folder_id='2', enable_debug=False)
        seen = {}
        api._build_url = lambda endpoint, params=None: seen.setdefault(endpoint, params) and None
        api._urlopen = lambda req: (_ for _ in ()).throw(ValueError('offline'))
        api._make_request('getAlbumList2', {'type': 'newest'})
        api._make_request('getAlbum', {'id': 'x'})
        self.assertEqual(seen['getAlbumList2']['musicFolderId'], '2')
        self.assertNotIn('musicFolderId', seen['getAlbum'])

    def test_sonic_match_parsed_per_spec(self):
        api = bare_api(os_extensions={navidrome_api.EXT_SONIC_SIMILARITY})
        api._make_request = lambda e, p=None: {
            'sonicMatch': [{'entry': {'id': '1'}, 'similarity': 0.9}]}
        self.assertEqual(api.get_sonic_similar_tracks('x'), [{'id': '1'}])

    def test_report_playback_uses_spec_params(self):
        api = bare_api(os_extensions={navidrome_api.EXT_PLAYBACK_REPORT})
        sent = []
        api._make_request = lambda e, p=None: sent.append((e, p)) or {}
        api.report_state('t1', 'paused', 1234.5)
        self.assertEqual(sent, [('reportPlayback', {
            'mediaId': 't1', 'mediaType': 'song', 'positionMs': 1234,
            'state': 'paused', 'ignoreScrobble': 'true'})])

    def test_account_slots(self):
        addon = FakeAddon(active_account='2', server_url='http://a', username='u',
                          password='p', server_url_2='http://b', username_2='v',
                          password_2='q', music_folder_2='3')
        self.assertEqual(navidrome_api.active_slot(addon), 2)
        acc = navidrome_api.account_settings(addon, 2)
        self.assertEqual((acc['server_url'], acc['music_folder_id'], acc['name']),
                         ('http://b', '3', 'v@http://b'))
        self.assertFalse(navidrome_api.account_configured(
            navidrome_api.account_settings(addon, 3)))
        self.assertEqual(navidrome_api.active_slot(FakeAddon(active_account='9')), 1)


if __name__ == '__main__':
    unittest.main()
