import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

# Import our API wrapper
from lib import library_sync, vfs
from lib.navidrome_api import (
    ACCOUNT_SLOTS, NavidromeAPI, account_configured, account_settings, active_slot,
    slot_suffix,
)

ADDON = xbmcaddon.Addon()
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_HANDLE = int(sys.argv[1])
# Fixed, not sys.argv[0]: library songs are invoked through the /library/ path.
BASE_URL = 'plugin://plugin.kodi.navidrome/'

PAGE_SIZES = [50, 100, 200, 500, 1000]
DEFAULT_PAGE_SIZE = 500

DOWNLOAD_CHUNK = 256 * 1024

RATING_LABELS = [
    'No rating',
    '* (1)',
    '** (2)',
    '*** (3)',
    '**** (4)',
    '***** (5)',
]


def current_account():
    return account_settings(ADDON, active_slot(ADDON))


def get_api():
    """API instance for the active account and library."""
    account = current_account()
    if not account_configured(account):
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Please configure server settings',
            xbmcgui.NOTIFICATION_WARNING
        )
        return None

    return NavidromeAPI(account['server_url'], account['username'],
                        account['password'], account['music_folder_id'])


def get_page_size():
    """
    Items per page, tolerant of how the setting has been stored.

    Older builds declared this as ``type="enum"``, which persists the *index*
    of the chosen entry, so a stored "4" has to keep meaning 1000.
    """
    raw = (ADDON.getSetting('items_per_page') or '').strip()
    if not raw:
        return DEFAULT_PAGE_SIZE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PAGE_SIZE
    if value in PAGE_SIZES:
        return value
    if 0 <= value < len(PAGE_SIZES):
        return PAGE_SIZES[value]
    return DEFAULT_PAGE_SIZE


def offline_enabled():
    try:
        return ADDON.getSettingBool('enable_offline_cache')
    except Exception:
        return True


def get_cache():
    """Shared xbmcvfs-backed offline media cache."""
    try:
        max_mb = int(ADDON.getSetting('cache_max_size') or '2000')
    except ValueError:
        max_mb = 2000
    return vfs.MediaCache(max_bytes=max_mb * 1024 * 1024)


def build_url(query):
    return BASE_URL + "?" + urllib.parse.urlencode(query)


def root_menu():
    """Main menu matching Navidrome structure"""
    items = [
        ("Albums", {"action": "albums_menu"}),
        ("Artists", {"action": "artists"}),
        ("Genres", {"action": "genres"}),
        ("Songs", {"action": "songs"}),
        ("Radios", {"action": "radios"}),
        ("Playlists", {"action": "playlists"}),
        ("Search", {"action": "search"}),
    ]

    if offline_enabled():
        items.append(("Offline", {"action": "offline"}))

    for label, query in items:
        url = build_url(query)
        li = xbmcgui.ListItem(label=label)
        # Use getMusicInfoTag() instead of setInfo()
        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(label)
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True,
        )

    # Pickers open a dialog instead of a folder, so they are not folders.
    account = current_account()
    pickers = [(f"Library: {account['music_folder_name'] or 'All'}", "choose_library")]
    configured = [s for s in range(1, ACCOUNT_SLOTS + 1)
                  if account_configured(account_settings(ADDON, s))]
    if len(configured) > 1:
        pickers.append((f"Account: {account['name']}", "switch_account"))
    for label, action in pickers:
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=build_url({"action": action}),
            listitem=xbmcgui.ListItem(label=label),
            isFolder=False,
        )

    # The labels above change with settings: never serve this from cache.
    xbmcplugin.endOfDirectory(ADDON_HANDLE, cacheToDisc=False)


def albums_menu():
    """Albums submenu"""
    items = [
        ("All", {"action": "albums_all"}),
        ("Random", {"action": "albums_random"}),
        ("Favourites", {"action": "albums_favourites"}),
        ("Top Rated", {"action": "albums_top_rated"}),
        ("Recently Added", {"action": "albums_recent"}),
        ("Recently Played", {"action": "albums_recently_played"}),
        ("Most Played", {"action": "albums_most_played"}),
    ]

    for label, query in items:
        url = build_url(query)
        li = xbmcgui.ListItem(label=label)
        # Use getMusicInfoTag() instead of setInfo()
        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(label)
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True,
        )

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# Pagination helpers
def add_load_more_item(action, offset, limit, **extra_params):
    """Add a 'Load More' item for pagination"""
    params = {"action": action, "offset": str(offset), "limit": str(limit)}
    params.update(extra_params)

    url = build_url(params)
    li = xbmcgui.ListItem(label="[Load More...]")
    music_tag = li.getMusicInfoTag()
    music_tag.setTitle("[Load More...]")
    # Keep the entry at the end of the list whatever sort the skin applies,
    # otherwise it gets sorted into the middle of the albums.
    li.setProperty('SpecialSort', 'bottom')

    xbmcplugin.addDirectoryItem(
        handle=ADDON_HANDLE,
        url=url,
        listitem=li,
        isFolder=True
    )


def end_paged_directory(api, action, offset, limit, shown, content,
                        sort_methods=(), extra_params=None):
    """
    Close a paginated directory, adding a 'Load More' entry when the server
    still has rows left.

    The next offset advances by the number of items actually rendered, never
    by the requested page size — the two differ whenever the server caps the
    response, and using the requested size silently skips whole blocks of the
    library.
    """
    total = getattr(api, 'last_total_count', 0) or 0
    next_offset = offset + shown

    if total:
        has_more = next_offset < total
    else:
        has_more = shown >= limit

    if shown and has_more:
        add_load_more_item(action, next_offset, limit, **(extra_params or {}))

    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)
    for method in sort_methods:
        xbmcplugin.addSortMethod(ADDON_HANDLE, method)

    xbmcplugin.setContent(ADDON_HANDLE, content)
    # Paginated listings must not be served from Kodi's directory cache or
    # the next page can come back as a copy of the previous one.
    xbmcplugin.endOfDirectory(ADDON_HANDLE, cacheToDisc=False)


def empty_directory(message, offset=0):
    if offset == 0:
        xbmcgui.Dialog().notification(ADDON_NAME, message, xbmcgui.NOTIFICATION_INFO)
    xbmcplugin.endOfDirectory(ADDON_HANDLE, cacheToDisc=False)


# List item builders
def list_artists(offset=0, limit=None):
    """List all artists"""
    api = get_api()
    if not api:
        return

    limit = limit or get_page_size()

    # Test connection first
    if offset == 0 and not api.ping():
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Failed to connect to server',
            xbmcgui.NOTIFICATION_ERROR
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE, cacheToDisc=False)
        return

    artists = api.get_artists(size=limit, offset=offset)

    if not artists:
        empty_directory('No artists found', offset)
        return

    for artist in artists:
        artist_id = artist.get('id')
        name = artist.get('name', 'Unknown Artist')

        url = build_url({"action": "artist", "id": artist_id})
        li = xbmcgui.ListItem(label=name)

        # Set artist info - use getMusicInfoTag()
        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(name)
        music_tag.setArtist(name)
        music_tag.setMediaType('artist')

        rating = NavidromeAPI.get_item_rating(artist)
        if rating:
            music_tag.setUserRating(rating * 2)

        # Add cover art if available
        cover_art = artist.get('coverArt')
        if cover_art:
            art_url = api.get_cover_art_url(cover_art)
            li.setArt({"thumb": art_url, "fanart": art_url})

        li.addContextMenuItems(rating_context_items(artist_id, 'artist', name))

        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )

    end_paged_directory(
        api, 'artists', offset, limit, len(artists), 'artists',
        sort_methods=(xbmcplugin.SORT_METHOD_ARTIST,)
    )


def list_artist_albums(artist_id):
    """List albums for a specific artist"""
    api = get_api()
    if not api:
        return

    artist = api.get_artist(artist_id)

    if not artist:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Failed to load artist',
            xbmcgui.NOTIFICATION_ERROR
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    li = xbmcgui.ListItem(label='Top Songs')
    li.setProperty('SpecialSort', 'top')
    xbmcplugin.addDirectoryItem(
        handle=ADDON_HANDLE,
        url=build_url({"action": "top_songs", "id": artist_id,
                       "name": artist.get('name', '')}),
        listitem=li,
        isFolder=True
    )

    for album in artist.get('album', []):
        add_album_item(api, album)

    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ALBUM)
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def rating_context_items(item_id, item_type, name):
    """Context entry for the Subsonic setRating endpoint."""
    if not item_id:
        return []
    return [(
        'Set Rating',
        'RunPlugin({})'.format(build_url({
            "action": "set_rating",
            "id": item_id,
            "type": item_type,
            "name": name,
        }))
    )]


def add_album_item(api, album):
    """Helper function to add an album list item"""
    album_id = album.get('id')
    title = album.get('name', 'Unknown Album')
    artist_name = album.get('artist') or album.get('albumArtist', 'Unknown Artist')
    artist_id = album.get('artistId')
    year = int(album.get('year', 0)) if album.get('year') else 0  # Convert to int
    starred = album.get('starred') is not None
    rating = NavidromeAPI.get_item_rating(album)

    url = build_url({"action": "album", "id": album_id})
    li = xbmcgui.ListItem(label=title)

    # Set album info - use getMusicInfoTag()
    music_tag = li.getMusicInfoTag()
    music_tag.setTitle(title)
    music_tag.setAlbum(title)
    music_tag.setArtist(artist_name)
    if year > 0:  # Only set year if valid
        music_tag.setYear(year)
    music_tag.setMediaType('album')
    if rating:
        # Kodi stores music user ratings on a 0-10 scale, Subsonic uses 0-5.
        music_tag.setUserRating(rating * 2)

    # Add cover art
    cover_art = album.get('coverArt')
    if cover_art:
        art_url = api.get_cover_art_url(cover_art)
        li.setArt({"thumb": art_url, "fanart": art_url})

    # Build context menu
    context_menu = []

    # Star/Unstar
    if starred:
        context_menu.append((
            'Unstar',
            f'RunPlugin({build_url({"action": "unstar", "id": album_id, "type": "album", "name": title})})'
        ))
    else:
        context_menu.append((
            'Star',
            f'RunPlugin({build_url({"action": "star", "id": album_id, "type": "album", "name": title})})'
        ))

    context_menu.extend(rating_context_items(album_id, 'album', title))

    if offline_enabled():
        context_menu.append((
            'Download album for offline',
            f'RunPlugin({build_url({"action": "download_album", "id": album_id, "name": title})})'
        ))

    # Go to Artist
    if artist_id:
        context_menu.append((
            f'Go to Artist: {artist_name}',
            f'Container.Update({build_url({"action": "artist", "id": artist_id})})'
        ))

    li.addContextMenuItems(context_menu)

    xbmcplugin.addDirectoryItem(
        handle=ADDON_HANDLE,
        url=url,
        listitem=li,
        isFolder=True
    )


def list_album_tracks(album_id):
    """List tracks for a specific album"""
    api = get_api()
    if not api:
        return

    album = api.get_album(album_id)

    if not album:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Failed to load album',
            xbmcgui.NOTIFICATION_ERROR
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    tracks = album.get('song', [])

    cache = get_cache() if offline_enabled() else None
    for track in tracks:
        add_track_item(api, track, cache)

    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_TRACKNUM)
    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def mime_type_for(suffix):
    """MIME type of a format, so Kodi need not ask the server for it."""
    return {
        'flac': 'audio/flac',
        'mp3': 'audio/mpeg',
        'opus': 'audio/ogg',
        'ogg': 'audio/ogg',
        'aac': 'audio/aac',
        'm4a': 'audio/mp4',
        'alac': 'audio/mp4',
        'wav': 'audio/wav',
        'wma': 'audio/x-ms-wma',
        'ape': 'audio/x-monkeys-audio',
    }.get((suffix or '').lower(), 'audio/flac')


def add_track_item(api, track, cache=None):
    """Helper function to add a track list item with proper codec info"""
    track_id = track.get('id')

    # Handle both native API and Subsonic API formats
    title = track.get('title') or track.get('name', 'Unknown Track')
    artist = track.get('artist') or track.get('artistName', 'Unknown Artist')
    album_name = track.get('album') or track.get('albumName', 'Unknown Album')
    duration = int(track.get('duration', 0))  # Convert to int
    track_number = track.get('track') or track.get('trackNumber', 0)
    year = int(track.get('year', 0)) if track.get('year') else 0  # Convert to int
    artist_id = track.get('artistId')
    album_id = track.get('albumId')
    starred = track.get('starred') is not None
    rating = NavidromeAPI.get_item_rating(track)

    # Get audio format info
    suffix = (track.get('suffix') or 'flac').lower()

    cached_path = cache.get(track_id) if cache else None

    # Play through the plugin so playback always resolves via the addon —
    # that is what lets an offline copy be substituted for the HTTP stream.
    play_url = build_url({"action": "play_track", "id": track_id})

    label = title
    if track_number and ADDON.getSettingBool('show_track_numbers'):
        label = f"{int(track_number):02d}. {title}"
    li = xbmcgui.ListItem(label=label)

    # Use modern getMusicInfoTag() instead of deprecated setInfo()
    music_tag = li.getMusicInfoTag()
    music_tag.setTitle(title)
    music_tag.setArtist(artist)
    music_tag.setAlbum(album_name)
    music_tag.setDuration(duration)  # Now it's an int
    music_tag.setTrack(track_number)
    music_tag.setDisc(int(track.get('discNumber') or 1))
    if year > 0:  # Only set year if valid
        music_tag.setYear(year)
    music_tag.setMediaType('song')
    if rating:
        # Kodi stores music user ratings on a 0-10 scale, Subsonic uses 0-5.
        music_tag.setUserRating(rating * 2)

    # Set MIME type to ensure PAPlayer is used
    li.setMimeType(mime_type_for(suffix))

    # Set ContentLookup to False to help with buffering
    li.setContentLookup(False)

    # Add cover art - handle both native and Subsonic API, plus offline sidecars
    art_url = track.get('coverArtUrl')
    if not art_url and api is not None:
        cover_art = None
        if track.get('coverArt'):
            cover_art = track.get('coverArt')
        elif track.get('coverArtId'):
            cover_art = track.get('coverArtId')
        elif track.get('hasCoverArt') and track.get('albumId'):
            cover_art = track.get('albumId')
        if cover_art:
            art_url = api.get_cover_art_url(cover_art)

    if art_url:
        li.setArt({"thumb": art_url})

    # Build context menu
    context_menu = []

    # Star/Unstar
    if starred:
        context_menu.append((
            'Unstar',
            f'RunPlugin({build_url({"action": "unstar", "id": track_id, "type": "song", "name": title})})'
        ))
    else:
        context_menu.append((
            'Star',
            f'RunPlugin({build_url({"action": "star", "id": track_id, "type": "song", "name": title})})'
        ))

    context_menu.extend(rating_context_items(track_id, 'song', title))

    # Add to Playlist
    context_menu.append((
        'Add to Playlist',
        f'RunPlugin({build_url({"action": "add_to_playlist", "id": track_id, "name": title})})'
    ))

    # Offline cache
    if offline_enabled():
        if cached_path:
            context_menu.append((
                'Remove download',
                f'RunPlugin({build_url({"action": "remove_download", "id": track_id, "name": title})})'
            ))
        else:
            context_menu.append((
                'Download for offline',
                f'RunPlugin({build_url({"action": "download_track", "id": track_id, "name": title})})'
            ))

    # Similar Songs (sonicSimilarity ext if available, else Instant Mix)
    context_menu.append((
        'Similar Songs',
        f'Container.Update({build_url({"action": "similar_songs", "id": track_id, "name": title})})'
    ))

    # Go to Album
    if album_id:
        context_menu.append((
            f'Go to Album: {album_name}',
            f'Container.Update({build_url({"action": "album", "id": album_id})})'
        ))

    # Go to Artist
    if artist_id:
        context_menu.append((
            f'Go to Artist: {artist}',
            f'Container.Update({build_url({"action": "artist", "id": artist_id})})'
        ))

    li.addContextMenuItems(context_menu)

    # Mark as playable
    li.setProperty('IsPlayable', 'true')
    if cached_path:
        li.setProperty('navidrome_offline', 'true')

    xbmcplugin.addDirectoryItem(
        handle=ADDON_HANDLE,
        url=play_url,
        listitem=li,
        isFolder=False
    )


def list_albums_all(offset=0, limit=None):
    """List all albums with pagination"""
    api = get_api()
    if not api:
        return

    limit = limit or get_page_size()
    albums = api.get_album_list('alphabeticalByName', size=limit, offset=offset)

    if not albums:
        empty_directory('No albums found', offset)
        return

    for album in albums:
        add_album_item(api, album)

    end_paged_directory(
        api, 'albums_all', offset, limit, len(albums), 'albums',
        sort_methods=(xbmcplugin.SORT_METHOD_ALBUM, xbmcplugin.SORT_METHOD_ARTIST)
    )


def list_albums_random(offset=0, limit=None):
    """List random albums with pagination"""
    api = get_api()
    if not api:
        return

    limit = limit or get_page_size()
    albums = api.get_album_list('random', size=limit, offset=offset)

    if not albums:
        empty_directory('No albums found', offset)
        return

    for album in albums:
        add_album_item(api, album)

    end_paged_directory(api, 'albums_random', offset, limit, len(albums), 'albums')


def list_albums_by_type(action, list_type, empty_message, offset=0, limit=None):
    """Shared implementation for the sorted album views."""
    api = get_api()
    if not api:
        return

    limit = limit or get_page_size()

    if list_type == 'starred':
        albums = api.get_starred_albums(size=limit, offset=offset)
    else:
        albums = api.get_album_list(list_type, size=limit, offset=offset)

    if not albums:
        empty_directory(empty_message, offset)
        return

    for album in albums:
        add_album_item(api, album)

    end_paged_directory(api, action, offset, limit, len(albums), 'albums')


def list_songs(offset=0, limit=None):
    """List all songs with pagination"""
    api = get_api()
    if not api:
        return

    limit = limit or get_page_size()
    songs = api.get_all_songs(size=limit, offset=offset)

    if not songs:
        empty_directory('No songs found', offset)
        return

    cache = get_cache() if offline_enabled() else None
    for track in songs:
        add_track_item(api, track, cache)

    end_paged_directory(
        api, 'songs', offset, limit, len(songs), 'songs',
        sort_methods=(
            xbmcplugin.SORT_METHOD_TITLE,
            xbmcplugin.SORT_METHOD_ARTIST,
            xbmcplugin.SORT_METHOD_ALBUM,
        )
    )


def list_radios():
    """List internet radio stations"""
    api = get_api()
    if not api:
        return

    radios = api.get_internet_radios()

    if not radios:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'No radio stations found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for radio in radios:
        name = radio.get('name', 'Unknown Radio')
        stream_url = radio.get('streamUrl', '')
        homepage = radio.get('homePageUrl', '')

        li = xbmcgui.ListItem(label=name)
        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(name)
        music_tag.setComment(homepage)
        music_tag.setMediaType('song')

        # Set MIME type for radio streams
        li.setMimeType('audio/mpeg')  # Most radio streams are MP3

        # Mark as playable
        li.setProperty('IsPlayable', 'true')

        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=stream_url,
            listitem=li,
            isFolder=False
        )

    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_playlists():
    """List all playlists"""
    api = get_api()
    if not api:
        return

    playlists = api.get_playlists()

    if not playlists:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'No playlists found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for playlist in playlists:
        playlist_id = playlist.get('id')
        name = playlist.get('name', 'Unknown Playlist')
        song_count = playlist.get('songCount', 0)

        url = build_url({"action": "playlist", "id": playlist_id})
        li = xbmcgui.ListItem(label=f"{name} ({song_count} tracks)")

        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(name)
        music_tag.setMediaType('playlist')

        # Add cover art if available
        cover_art = playlist.get('coverArt')
        if cover_art:
            art_url = api.get_cover_art_url(cover_art)
            li.setArt({"thumb": art_url})

        if offline_enabled():
            li.addContextMenuItems([(
                'Download playlist for offline',
                f'RunPlugin({build_url({"action": "download_playlist", "id": playlist_id, "name": name})})'
            )])

        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )

    xbmcplugin.setContent(ADDON_HANDLE, 'playlists')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_similar_songs(track_id, track_name=''):
    """List sonically/metadata-similar songs for a track."""
    api = get_api()
    if not api:
        return

    # Prefers sonicSimilarity extension (v0.62), falls back to Instant Mix
    songs = api.get_sonic_similar_tracks(track_id)

    if not songs:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'No similar songs found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    cache = get_cache() if offline_enabled() else None
    for track in songs:
        add_track_item(api, track, cache)

    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_top_songs(artist_id, artist_name):
    """Most played songs of an artist, as known to Last.fm via the server."""
    api = get_api()
    if not api:
        return

    songs = api.get_top_songs(artist_id, artist_name)
    if not songs:
        empty_directory('No top songs (the server needs Last.fm configured)')
        return

    cache = get_cache() if offline_enabled() else None
    for track in songs:
        add_track_item(api, track, cache)

    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def switch_account():
    """Pick which configured account the addon browses and scrobbles as."""
    accounts = [account_settings(ADDON, s) for s in range(1, ACCOUNT_SLOTS + 1)]
    accounts = [a for a in accounts if account_configured(a)]
    if not accounts:
        return
    current = active_slot(ADDON)
    labels = [('* ' if a['slot'] == current else '') + a['name'] for a in accounts]
    selected = xbmcgui.Dialog().select('Switch account', labels)
    if selected < 0 or accounts[selected]['slot'] == current:
        return
    ADDON.setSetting('active_account', str(accounts[selected]['slot']))
    vfs.clear_session()
    xbmcgui.Dialog().notification(
        ADDON_NAME, f"Account: {accounts[selected]['name']}", xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin('Container.Refresh')


def library_sync_enabled():
    try:
        return ADDON.getSettingBool('library_sync')
    except Exception:
        return False


def sync_library_action(remove=False):
    """Copy the Navidrome library into Kodi's Music library, or take it out."""
    progress = xbmcgui.DialogProgressBG()
    progress.create(ADDON_NAME, 'Removing from Kodi library' if remove else 'Syncing Kodi library')
    try:
        if remove:
            result = library_sync.remove()
            message = f"Removed {result['removed']} songs from Kodi library"
        else:
            api = get_api()
            if not api:
                return
            result = library_sync.sync(
                api, lambda count: progress.update(50, message=f'{count} songs'))
            message = f"Kodi library: {result['songs']} songs, {result['albums']} albums"
    except Exception as exc:
        xbmc.log(f"NAVIDROME SYNC: failed: {exc!r}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification(
            ADDON_NAME, f'Library sync failed: {exc}', xbmcgui.NOTIFICATION_ERROR)
        return
    finally:
        progress.close()
    xbmcgui.Dialog().notification(ADDON_NAME, message, xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin('Container.Refresh')


def choose_library():
    """Restrict browsing to one Navidrome library (or all of them)."""
    api = get_api()
    if not api:
        return
    folders = api.get_music_folders()
    if not folders:
        xbmcgui.Dialog().notification(
            ADDON_NAME, 'Could not load libraries', xbmcgui.NOTIFICATION_ERROR)
        return

    labels = ['All libraries'] + [f.get('name') or str(f.get('id')) for f in folders]
    selected = xbmcgui.Dialog().select('Library', labels)
    if selected < 0:
        return

    sfx = slot_suffix(active_slot(ADDON))
    folder = folders[selected - 1] if selected else {}
    ADDON.setSetting(f'music_folder{sfx}', str(folder.get('id', '')))
    ADDON.setSetting(f'music_folder_name{sfx}', folder.get('name', '') if folder else '')
    xbmc.executebuiltin('Container.Refresh')


def list_playlist_tracks(playlist_id):
    """List tracks in a playlist"""
    api = get_api()
    if not api:
        return

    playlist = api.get_playlist(playlist_id)

    if not playlist:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Failed to load playlist',
            xbmcgui.NOTIFICATION_ERROR
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    tracks = playlist.get('entry', [])

    cache = get_cache() if offline_enabled() else None
    for track in tracks:
        add_track_item(api, track, cache)

    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def search():
    """Search for music"""
    dialog = xbmcgui.Dialog()
    query = dialog.input('Search')

    if not query:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    api = get_api()
    if not api:
        return

    results = api.search(query)

    if not results:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'No results found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Add artists
    for artist in results.get('artist', []):
        artist_id = artist.get('id')
        name = artist.get('name', 'Unknown Artist')

        url = build_url({"action": "artist", "id": artist_id})
        li = xbmcgui.ListItem(label=f"[Artist] {name}")

        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(name)
        music_tag.setArtist(name)
        music_tag.setMediaType('artist')

        cover_art = artist.get('coverArt')
        if cover_art:
            art_url = api.get_cover_art_url(cover_art)
            li.setArt({"thumb": art_url})

        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )

    # Add albums
    for album in results.get('album', []):
        add_album_item(api, album)

    # Add songs
    cache = get_cache() if offline_enabled() else None
    for track in results.get('song', []):
        add_track_item(api, track, cache)

    xbmcplugin.setContent(ADDON_HANDLE, 'mixed')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def star_item(item_id, item_type, item_name):
    """Star an item"""
    api = get_api()
    if not api:
        return

    if api.star(item_id, item_type):
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            f'Starred: {item_name}',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmc.executebuiltin('Container.Refresh')
    else:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Failed to star item',
            xbmcgui.NOTIFICATION_ERROR
        )


def unstar_item(item_id, item_type, item_name):
    """Unstar an item"""
    api = get_api()
    if not api:
        return

    if api.unstar(item_id, item_type):
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            f'Unstarred: {item_name}',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmc.executebuiltin('Container.Refresh')
    else:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Failed to unstar item',
            xbmcgui.NOTIFICATION_ERROR
        )


def set_rating_dialog(item_id, item_type, item_name):
    """Ask for a 0-5 rating and push it to the server (Subsonic setRating)."""
    if not item_id:
        return

    selected = xbmcgui.Dialog().select(f'Rate: {item_name}', RATING_LABELS)
    if selected < 0:
        return

    api = get_api()
    if not api:
        return

    if api.set_rating(item_id, selected):
        message = 'Rating cleared' if selected == 0 else f'Rated {selected}/5'
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            f'{message}: {item_name}',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmc.executebuiltin('Container.Refresh')
    else:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'Failed to set rating',
            xbmcgui.NOTIFICATION_ERROR
        )


def add_to_playlist_dialog(track_id, track_name):
    """Show dialog to add track to playlist"""
    api = get_api()
    if not api:
        return

    playlists = api.get_playlists()

    if not playlists:
        # Create new playlist
        dialog = xbmcgui.Dialog()
        playlist_name = dialog.input('Create New Playlist')
        if playlist_name:
            result = api.create_playlist(playlist_name, [track_id])
            if result:
                xbmcgui.Dialog().notification(
                    ADDON_NAME,
                    f'Created playlist: {playlist_name}',
                    xbmcgui.NOTIFICATION_INFO
                )
        return

    # Show playlist selection
    playlist_names = ['[Create New Playlist]'] + [p.get('name', 'Unknown') for p in playlists]
    dialog = xbmcgui.Dialog()
    selected = dialog.select('Add to Playlist', playlist_names)

    if selected < 0:
        return

    if selected == 0:
        # Create new playlist
        playlist_name = dialog.input('Create New Playlist')
        if playlist_name:
            result = api.create_playlist(playlist_name, [track_id])
            if result:
                xbmcgui.Dialog().notification(
                    ADDON_NAME,
                    f'Added to new playlist: {playlist_name}',
                    xbmcgui.NOTIFICATION_INFO
                )
    else:
        # Add to existing playlist
        playlist = playlists[selected - 1]
        playlist_id = playlist.get('id')
        if api.update_playlist(playlist_id, [track_id]):
            xbmcgui.Dialog().notification(
                ADDON_NAME,
                f'Added to: {playlist.get("name")}',
                xbmcgui.NOTIFICATION_INFO
            )
        else:
            xbmcgui.Dialog().notification(
                ADDON_NAME,
                'Failed to add to playlist',
                xbmcgui.NOTIFICATION_ERROR
            )


def list_genres():
    """List all genres"""
    api = get_api()
    if not api:
        return

    genres = api.get_genres()

    if not genres:
        xbmcgui.Dialog().notification(
            ADDON_NAME,
            'No genres found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for genre in genres:
        genre_name = genre.get('value', 'Unknown')
        song_count = genre.get('songCount', 0)
        album_count = genre.get('albumCount', 0)

        url = build_url({"action": "genre", "name": genre_name})
        li = xbmcgui.ListItem(label=f"{genre_name} ({album_count} albums, {song_count} songs)")

        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(genre_name)
        # Don't use setGenre() - it doesn't exist
        # Optionally use setGenres() with a list if needed:
        # music_tag.setGenres([genre_name])

        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )

    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    xbmcplugin.setContent(ADDON_HANDLE, 'genres')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_genre_content(genre_name):
    """Show albums and songs for a genre"""
    items = [
        (f"Albums ({genre_name})", {"action": "genre_albums", "name": genre_name}),
        (f"Songs ({genre_name})", {"action": "genre_songs", "name": genre_name}),
    ]

    for label, query in items:
        url = build_url(query)
        li = xbmcgui.ListItem(label=label)
        music_tag = li.getMusicInfoTag()
        music_tag.setTitle(label)
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_genre_albums(genre_name, offset=0, limit=None):
    """List albums for a genre"""
    api = get_api()
    if not api:
        return

    limit = limit or get_page_size()
    albums = api.get_albums_by_genre(genre_name, size=limit, offset=offset)

    if not albums:
        empty_directory(f'No albums found for {genre_name}', offset)
        return

    for album in albums:
        add_album_item(api, album)

    end_paged_directory(
        api, 'genre_albums', offset, limit, len(albums), 'albums',
        extra_params={"name": genre_name}
    )


def list_genre_songs(genre_name, offset=0, limit=None):
    """List songs for a genre"""
    api = get_api()
    if not api:
        return

    limit = limit or get_page_size()
    songs = api.get_songs_by_genre(genre_name, size=limit, offset=offset)

    if not songs:
        empty_directory(f'No songs found for {genre_name}', offset)
        return

    cache = get_cache() if offline_enabled() else None
    for track in songs:
        add_track_item(api, track, cache)

    end_paged_directory(
        api, 'genre_songs', offset, limit, len(songs), 'songs',
        extra_params={"name": genre_name}
    )


# Offline cache (xbmcvfs)
def track_metadata(api, track):
    """Metadata sidecar for a cached track, enough to browse it offline."""
    cover_art = track.get('coverArt') or track.get('coverArtId')
    return {
        'title': track.get('title') or track.get('name', 'Unknown Track'),
        'artist': track.get('artist') or track.get('artistName', 'Unknown Artist'),
        'album': track.get('album') or track.get('albumName', 'Unknown Album'),
        'albumId': track.get('albumId'),
        'artistId': track.get('artistId'),
        'duration': int(track.get('duration', 0) or 0),
        'track': track.get('track') or track.get('trackNumber', 0),
        'year': track.get('year') or 0,
        'suffix': (track.get('suffix') or 'mp3').lower(),
        'coverArtUrl': api.get_cover_art_url(cover_art) if cover_art else None,
    }


def download_track(api, cache, track, progress=None, heading=''):
    """Copy one track into the offline cache through xbmcvfs."""
    track_id = track.get('id')
    if not track_id:
        return False
    if cache.has(track_id):
        return True

    response, fmt = api.open_stream(track_id)
    if response is None:
        return False
    suffix = fmt or track.get('suffix') or 'mp3'

    handle, path = cache.open_writer(track_id, suffix)
    if handle is None:
        response.close()
        return False

    try:
        total = int(response.headers.get('Content-Length') or 0)
        written = 0
        while True:
            chunk = response.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            handle.write(bytearray(chunk))
            written += len(chunk)
            if progress is not None and total:
                progress.update(int(written * 100 / total), message=heading)
    except Exception as exc:
        xbmc.log(f"NAVIDROME: download failed for {track_id}: {exc}", xbmc.LOGERROR)
        handle.close()
        response.close()
        vfs.delete(path)
        return False
    finally:
        try:
            handle.close()
        except Exception:
            pass
        try:
            response.close()
        except Exception:
            pass

    return cache.commit(track_id, path, track_metadata(api, track)) is not None


def resolve_track(api, track_id):
    """Fetch a single track's metadata, falling back to the bare id."""
    return api.get_song(track_id) or {'id': track_id}


def download_track_action(track_id, track_name):
    api = get_api()
    if not api:
        return

    cache = get_cache()
    progress = xbmcgui.DialogProgressBG()
    progress.create(ADDON_NAME, f'Downloading {track_name}')
    try:
        track = resolve_track(api, track_id)
        ok = download_track(api, cache, track, progress, track_name or '')
    finally:
        progress.close()

    if ok:
        cache.prune()
        xbmcgui.Dialog().notification(
            ADDON_NAME, f'Downloaded: {track_name}', xbmcgui.NOTIFICATION_INFO)
        xbmc.executebuiltin('Container.Refresh')
    else:
        xbmcgui.Dialog().notification(
            ADDON_NAME, f'Download failed: {track_name}', xbmcgui.NOTIFICATION_ERROR)


def download_many(tracks, heading):
    api = get_api()
    if not api:
        return

    tracks = [t for t in tracks if t.get('id')]
    if not tracks:
        xbmcgui.Dialog().notification(
            ADDON_NAME, 'Nothing to download', xbmcgui.NOTIFICATION_INFO)
        return

    cache = get_cache()
    monitor = xbmc.Monitor()
    progress = xbmcgui.DialogProgressBG()
    progress.create(ADDON_NAME, heading)

    done = 0
    try:
        for index, track in enumerate(tracks):
            if monitor.abortRequested():
                break
            label = track.get('title') or track.get('name') or ''
            progress.update(int(index * 100 / len(tracks)), message=label)
            if download_track(api, cache, track):
                done += 1
    finally:
        progress.close()

    cache.prune()
    xbmcgui.Dialog().notification(
        ADDON_NAME,
        f'Downloaded {done}/{len(tracks)} tracks',
        xbmcgui.NOTIFICATION_INFO
    )
    xbmc.executebuiltin('Container.Refresh')


def download_album_action(album_id, album_name):
    api = get_api()
    if not api:
        return
    album = api.get_album(album_id)
    if not album:
        xbmcgui.Dialog().notification(
            ADDON_NAME, 'Failed to load album', xbmcgui.NOTIFICATION_ERROR)
        return
    download_many(album.get('song', []), f'Downloading {album_name}')


def download_playlist_action(playlist_id, playlist_name):
    api = get_api()
    if not api:
        return
    playlist = api.get_playlist(playlist_id)
    if not playlist:
        xbmcgui.Dialog().notification(
            ADDON_NAME, 'Failed to load playlist', xbmcgui.NOTIFICATION_ERROR)
        return
    download_many(playlist.get('entry', []), f'Downloading {playlist_name}')


def remove_download_action(track_id, track_name):
    get_cache().remove(track_id)
    xbmcgui.Dialog().notification(
        ADDON_NAME, f'Removed download: {track_name}', xbmcgui.NOTIFICATION_INFO)
    xbmc.executebuiltin('Container.Refresh')


def list_offline():
    """Browse tracks held in the offline cache."""
    cache = get_cache()
    entries = cache.entries()

    if not entries:
        xbmcgui.Dialog().notification(
            ADDON_NAME, 'No offline tracks', xbmcgui.NOTIFICATION_INFO)
        xbmcplugin.endOfDirectory(ADDON_HANDLE, cacheToDisc=False)
        return

    # No API instance here on purpose: the point of this view is that it works
    # with the server unreachable. The sidecars carry everything we render.
    for meta in entries:
        add_track_item(None, meta, cache)

    used_mb = cache.total_size() / (1024 * 1024)
    xbmc.log(
        f"NAVIDROME: offline cache holds {len(entries)} tracks ({used_mb:.1f} MB)",
        xbmc.LOGINFO
    )

    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ARTIST)
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ALBUM)
    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE, cacheToDisc=False)


def clear_cache_action():
    cache = get_cache()
    used_mb = cache.total_size() / (1024 * 1024)
    if not xbmcgui.Dialog().yesno(
        ADDON_NAME,
        f'Delete all offline tracks? ({used_mb:.1f} MB)'
    ):
        return
    cache.clear()
    vfs.clear_session()
    xbmcgui.Dialog().notification(
        ADDON_NAME, 'Offline cache cleared', xbmcgui.NOTIFICATION_INFO)


def play_track(track_id):
    """Resolve and play a track, preferring an offline copy when we have one."""
    if not track_id:
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())
        return

    path = None
    suffix = None

    if offline_enabled():
        cache = get_cache()
        path = cache.get(track_id)
        if path:
            suffix = path.rsplit('.', 1)[-1]

    api = None
    if not path:
        api = get_api()
        if not api:
            xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())
            return
        path, suffix = api.get_stream(track_id)

    try:
        if ADDON.getSettingBool('enable_debug'):
            xbmc.log(f"NAVIDROME: Resolving track {track_id} to {path}", xbmc.LOGINFO)

        # Let the service identify the track even when playback resolves to a
        # local special:// path with no id in it.
        vfs.set_now_playing(track_id, path)

        play_item = xbmcgui.ListItem(path=path)
        play_item.setContentLookup(False)
        # Only assert a MIME type when it is actually known — for an untranscoded
        # stream the container is whatever the server stored, so let Kodi sniff.
        if suffix:
            play_item.setMimeType(mime_type_for(suffix))
        play_item.setProperty('navidrome_id', track_id)

        xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, play_item)

    except Exception as e:
        xbmc.log(f"NAVIDROME: Error resolving track: {str(e)}", xbmc.LOGERROR)
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())


def router(paramstring):
    """Route to the appropriate function"""
    params = dict(urllib.parse.parse_qsl(paramstring))
    action = params.get("action")

    def page_args():
        try:
            offset = int(params.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(params.get("limit", 0)) or None
        except (TypeError, ValueError):
            limit = None
        return offset, limit

    library_path = urllib.parse.unquote(urllib.parse.urlparse(sys.argv[0]).path)
    if library_path.startswith('/library'):
        # Kodi's Music library (see lib/library_sync.py): a song there is
        # /library/<id>.<suffix>; the folder itself is listed by Kodi's
        # library scanner and must look empty.
        song_id = library_sync.song_id_from_library_path(library_path)
        if song_id:
            play_track(song_id)
        else:
            xbmcplugin.endOfDirectory(ADDON_HANDLE, cacheToDisc=False)
    elif action is None:
        root_menu()
    elif action == "albums_menu":
        albums_menu()
    elif action == "albums_favourites":
        offset, limit = page_args()
        list_albums_by_type("albums_favourites", "starred",
                            "No favourite albums found", offset, limit)
    elif action == "albums_top_rated":
        offset, limit = page_args()
        list_albums_by_type("albums_top_rated", "highest",
                            "No albums found", offset, limit)
    elif action == "albums_recent":
        offset, limit = page_args()
        list_albums_by_type("albums_recent", "newest",
                            "No albums found", offset, limit)
    elif action == "albums_recently_played":
        offset, limit = page_args()
        list_albums_by_type("albums_recently_played", "recent",
                            "No albums found", offset, limit)
    elif action == "albums_most_played":
        offset, limit = page_args()
        list_albums_by_type("albums_most_played", "frequent",
                            "No albums found", offset, limit)
    elif action == "artists":
        offset, limit = page_args()
        list_artists(offset, limit)
    elif action == "artist":
        list_artist_albums(params.get("id"))
    elif action == "album":
        list_album_tracks(params.get("id"))
    elif action == "radios":
        list_radios()
    elif action == "playlists":
        list_playlists()
    elif action == "playlist":
        list_playlist_tracks(params.get("id"))
    elif action == "search":
        search()
    elif action == "star":
        star_item(params.get("id"), params.get("type"), params.get("name"))
    elif action == "unstar":
        unstar_item(params.get("id"), params.get("type"), params.get("name"))
    elif action == "set_rating":
        set_rating_dialog(params.get("id"), params.get("type", "song"), params.get("name", ''))
    elif action == "add_to_playlist":
        add_to_playlist_dialog(params.get("id"), params.get("name"))
    elif action == "similar_songs":
        list_similar_songs(params.get("id"), params.get("name"))
    elif action == "genres":
        list_genres()
    elif action == "genre":
        list_genre_content(params.get("name"))
    elif action == "genre_albums":
        offset, limit = page_args()
        list_genre_albums(params.get("name"), offset, limit)
    elif action == "genre_songs":
        offset, limit = page_args()
        list_genre_songs(params.get("name"), offset, limit)
    elif action == "albums_all":
        offset, limit = page_args()
        list_albums_all(offset, limit)
    elif action == "albums_random":
        offset, limit = page_args()
        list_albums_random(offset, limit)
    elif action == "songs":
        offset, limit = page_args()
        list_songs(offset, limit)
    elif action == "offline":
        list_offline()
    elif action == "download_track":
        download_track_action(params.get("id"), params.get("name", ''))
    elif action == "download_album":
        download_album_action(params.get("id"), params.get("name", ''))
    elif action == "download_playlist":
        download_playlist_action(params.get("id"), params.get("name", ''))
    elif action == "remove_download":
        remove_download_action(params.get("id"), params.get("name", ''))
    elif action == "clear_cache":
        clear_cache_action()
    elif action == "play_track":
        play_track(params.get("id"))
    elif action == "top_songs":
        list_top_songs(params.get("id"), params.get("name", ''))
    elif action == "switch_account":
        switch_account()
    elif action == "choose_library":
        choose_library()
    elif action == "sync_library":
        sync_library_action()
    elif action == "remove_library":
        sync_library_action(remove=True)
    else:
        xbmc.log(f"Unknown action: {action}", xbmc.LOGWARNING)
        root_menu()


if __name__ == "__main__":
    router(sys.argv[2][1:])
