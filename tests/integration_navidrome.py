"""
Integration test of lib/navidrome_api.py against a real Navidrome server.

Not run by unittest discovery (needs a server). Usage:
    ND_URL=http://127.0.0.1:4533 ND_USER=admin ND_PASS=secret \
        python3 tests/integration_navidrome.py
It stars, rates, scrobbles and creates a playlist: use a throwaway server.
"""
import json
import os
import sys
import types
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

xbmc = types.ModuleType('xbmc')
xbmc.LOGDEBUG = xbmc.LOGINFO = xbmc.LOGWARNING = xbmc.LOGERROR = 0
LOG = []
xbmc.log = lambda msg, level=0: LOG.append(msg)
sys.modules['xbmc'] = xbmc
sys.modules['xbmcvfs'] = types.ModuleType('xbmcvfs')


class Addon:
    settings = {'api_timeout': '10'}

    def __init__(self, *a):
        pass

    def getSetting(self, key):
        return self.settings.get(key, '')

    def getSettingBool(self, key):
        return self.settings.get(key) == 'true'


xbmcaddon = types.ModuleType('xbmcaddon')
xbmcaddon.Addon = Addon
sys.modules['xbmcaddon'] = xbmcaddon

from lib import navidrome_api, vfs  # noqa: E402

SESSION = {}
vfs.load_session = lambda key, max_age: SESSION.get(key)
vfs.save_session = lambda key, payload: SESSION.__setitem__(key, dict(payload, key=key))
vfs.clear_session = SESSION.clear

URL, USER, PASS = os.environ['ND_URL'], os.environ['ND_USER'], os.environ['ND_PASS']
FAILED = []


def check(name, condition, detail=''):
    print(('ok   ' if condition else 'FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not condition:
        FAILED.append(name)


def api(folder=None):
    return navidrome_api.NavidromeAPI(URL, USER, PASS, folder)


a = api()
check('native login', bool(a.native_token and a.subsonic_token))
check('ping', a.ping())
check('extensions detected', a.has_extension('playbackReport'), sorted(a.os_extensions))
check('session cached', bool(SESSION) and api().native_token == a.native_token)

songs = a.get_all_songs(size=500)
total = len(songs)
check('all songs (native)', total > 5 and a.last_total_count == total, f'{total} songs')
albums = a.get_album_list('alphabeticalByName')
check('albums', len(albums) > 1, f'{len(albums)} albums')
artists = a.get_artists()
check('artists', len(artists) > 1, f'{len(artists)} artists')

# Issue #3: pages bigger than the server cap, and Load More offsets.
navidrome_api.MAX_REQUEST_SIZE = 4
ids = [s['id'] for s in a.get_all_songs(size=total, offset=0)]
check('page assembled from several requests', ids == [s['id'] for s in songs])
page1 = a.get_all_songs(size=5, offset=0)
page2 = a.get_all_songs(size=5, offset=5)
check('second page differs and continues',
      [s['id'] for s in page1 + page2] == ids[:10])
check('total count for Load More', a.last_total_count == total, a.last_total_count)
a.native_token = None  # Subsonic fallback path
sub = a.get_all_songs(size=total, offset=0)
check('subsonic fallback returns whole library', len(sub) == total, len(sub))
check('subsonic fallback paging',
      len(a.get_album_list('alphabeticalByName', size=3, offset=1)) == min(3, len(albums) - 1))
navidrome_api.MAX_REQUEST_SIZE = 500
a = api()

# Kodi library sync source
navidrome_api.MAX_REQUEST_SIZE = 4
lib = a.get_library_songs()
check('library songs: every song once, Subsonic format',
      sorted(s['id'] for s in lib) == sorted(ids) and 'albumArtists' in lib[0] and 'suffix' in lib[0])
navidrome_api.MAX_REQUEST_SIZE = 500

for list_type in ('random', 'newest', 'highest', 'frequent', 'recent', 'alphabeticalByArtist'):
    check(f'album list {list_type}', len(a.get_album_list(list_type)) == len(albums))

album = a.get_album(albums[0]['id'])
check('album tracks', bool(album and album.get('song')))
artist = a.get_artist(artists[0]['id'])
check('artist albums', bool(artist and artist.get('album')))
track = album['song'][0]
check('get_song', a.get_song(track['id'])['id'] == track['id'])

genres = a.get_genres()
check('genres', len(genres) > 0, [g['value'] for g in genres])
g = genres[0]['value']
check('songs by genre', len(a.get_songs_by_genre(g)) == genres[0]['songCount'])
check('albums by genre', len(a.get_albums_by_genre(g)) == genres[0]['albumCount'])
found = a.search(track['title'])
check('search', any(s['id'] == track['id'] for s in found.get('song', [])))

# Libraries
folders = a.get_music_folders()
check('music folders', len(folders) >= 1, folders)
if len(folders) > 1:
    counts = []
    for folder in folders:
        f = api(folder['id'])
        n = len(f.get_all_songs())
        counts.append(n)
        fa = f.get_album_list('alphabeticalByName')
        check(f"library {folder['name']}: albums/artists/starred filtered",
              0 < len(fa) < len(albums) and 0 < len(f.get_artists()) < len(artists)
              and len(f.get_album_list('newest')) == len(fa)
              and len(f.get_album_list('random')) == len(fa),
              f'{n} songs, {len(fa)} albums')
        check(f"library {folder['name']}: search filtered",
              len(f.search('song').get('song', [])) <= n)
    check('libraries partition the songs', sum(counts) == total, counts)

# Stars and ratings (issue #4), read back through both API layers.
check('star song', a.star(track['id'], 'song') and a.get_song(track['id']).get('starred'))
check('unstar song', a.unstar(track['id'], 'song') and not a.get_song(track['id']).get('starred'))
check('star album', a.star(album['id'], 'album'))
check('starred albums list', [x['id'] for x in a.get_starred_albums()] == [album['id']])
f1 = api(folders[0]['id'])
check('starred albums with library filter', len(f1.get_starred_albums()) <= 1)
check('unstar album', a.unstar(album['id'], 'album') and a.get_starred_albums() == [])

check('rate song 4', a.set_rating(track['id'], 4))
check('rating via subsonic', a.get_item_rating(a.get_song(track['id'])) == 4)
native = [s for s in a.get_all_songs() if s['id'] == track['id']][0]
check('rating via native', a.get_item_rating(native) == 4)
check('rate album 5', a.set_rating(album['id'], 5))
check('album rating via native',
      a.get_item_rating([x for x in a.get_album_list('alphabeticalByName')
                         if x['id'] == album['id']][0]) == 5)
check('top rated first', a.get_album_list('highest')[0]['id'] == album['id'])
check('rate artist 3', a.set_rating(artists[0]['id'], 3))
check('artist rating via native',
      a.get_item_rating([x for x in a.get_artists() if x['id'] == artists[0]['id']][0]) == 3)
check('clear ratings', a.set_rating(track['id'], 0) and a.set_rating(album['id'], 0)
      and a.set_rating(artists[0]['id'], 0)
      and a.get_item_rating(a.get_song(track['id'])) == 0)

# Playlists
before = len(a.get_playlists())
check('create playlist', bool(a.create_playlist('kodi-it', [track['id']])))
pl = [p for p in a.get_playlists() if p['name'] == 'kodi-it'][-1]
check('playlist listed', len(a.get_playlists()) == before + 1)
check('add to playlist', a.update_playlist(pl['id'], [album['song'][1]['id']])
      and len(a.get_playlist(pl['id'])['entry']) == 2)
a._make_request('deletePlaylist', {'id': pl['id']})


# Playback reporting
def now_playing():
    r = a._make_request('getNowPlaying') or {}
    return (r.get('nowPlaying') or {}).get('entry', [])


plays_before = a.get_song(track['id']).get('playCount', 0)
check('report playing', a.report_state(track['id'], 'playing', 0))
np = now_playing()
check('shown in now playing', any(e['id'] == track['id'] for e in np),
      [(e.get('title'), e.get('state')) for e in np])
check('report paused', a.report_state(track['id'], 'paused', 3000))
check('paused state visible', any(e.get('state') == 'paused' for e in now_playing()))
check('report stopped', a.report_state(track['id'], 'stopped', 11000))
check('removed from now playing', not any(e['id'] == track['id'] for e in now_playing()))
check('stopped did not count a play',
      a.get_song(track['id']).get('playCount', 0) == plays_before)
check('scrobble', a.scrobble(track['id']))
check('exactly one play counted',
      a.get_song(track['id']).get('playCount', 0) == plays_before + 1)
a.os_extensions.discard('playbackReport')  # pre-0.62 server path
check('legacy now playing', a.report_state(track['id'], 'playing', 0)
      and any(e['id'] == track['id'] for e in now_playing()))
check('legacy now playing did not count a play',
      a.get_song(track['id']).get('playCount', 0) == plays_before + 1)
a = api()

# Similar / top songs: no Last.fm or plugin here, must degrade to [].
check('similar songs degrade', isinstance(a.get_sonic_similar_tracks(track['id']), list))
check('top songs degrade', a.get_top_songs(artists[0]['id'], artists[0]['name']) == [])
check('radios', a.get_internet_radios() == [])

# Media
stream = a.open_stream(track['id'])
data = stream.read()
stream.close()
check('stream download', data[:4] == b'fLaC', f'{len(data)} bytes')
cover = a.get_cover_art_url(album.get('coverArt') or album['id'])
with urllib.request.urlopen(cover.split('|')[0]) as r:
    check('cover art url', r.status == 200 and r.headers['Content-Type'].startswith('image/'),
          r.headers['Content-Type'])

# Wrong password must fail cleanly, not raise.
bad = navidrome_api.NavidromeAPI(URL, USER, PASS + 'x')
check('bad password handled', not bad.ping() and bad.get_all_songs() == [])
check('failed download is None, not an empty library', bad.get_library_songs() is None)

errors = [m for m in LOG if 'ERROR' in m]
print(f'\n{len(FAILED)} failed; logged errors: {json.dumps(errors[:8], indent=1)}')
sys.exit(1 if FAILED else 0)
