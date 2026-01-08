import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

# Import our API wrapper
from lib.navidrome_api import NavidromeAPI

ADDON = xbmcaddon.Addon()
ADDON_HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]


def get_api():
    """Get configured API instance"""
    server_url = ADDON.getSetting('server_url')
    username = ADDON.getSetting('username')
    password = ADDON.getSetting('password')
    
    if not server_url or not username or not password:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'Please configure server settings',
            xbmcgui.NOTIFICATION_WARNING
        )
        return None
    
    return NavidromeAPI(server_url, username, password)


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

    for label, query in items:
        url = build_url(query)
        li = xbmcgui.ListItem(label=label)
        li.setInfo("music", {"title": label})
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True,
        )

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


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
        li.setInfo("music", {"title": label})
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True,
        )

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_artists():
    """List all artists"""
    api = get_api()
    if not api:
        return
    
    # Test connection first
    if not api.ping():
        xbmcgui.Dialog().notification(
            'Navidrome',
            'Failed to connect to server',
            xbmcgui.NOTIFICATION_ERROR
        )
        return
    
    artists = api.get_artists()
    
    if not artists:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'No artists found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for artist in artists:
        artist_id = artist.get('id')
        name = artist.get('name', 'Unknown Artist')
        
        url = build_url({"action": "artist", "id": artist_id})
        li = xbmcgui.ListItem(label=name)
        
        # Set artist info
        li.setInfo("music", {
            "title": name,
            "artist": name,
            "mediatype": "artist"
        })
        
        # Add cover art if available
        cover_art = artist.get('coverArt')
        if cover_art:
            art_url = api.get_cover_art_url(cover_art)
            li.setArt({"thumb": art_url, "fanart": art_url})
        
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )
    
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ARTIST)
    xbmcplugin.setContent(ADDON_HANDLE, 'artists')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_artist_albums(artist_id):
    """List albums for a specific artist"""
    api = get_api()
    if not api:
        return
    
    artist = api.get_artist(artist_id)
    
    if not artist:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'Failed to load artist',
            xbmcgui.NOTIFICATION_ERROR
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    albums = artist.get('album', [])
    
    for album in albums:
        add_album_item(api, album)
    
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ALBUM)
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def add_album_item(api, album):
    """Helper function to add an album list item"""
    album_id = album.get('id')
    title = album.get('name', 'Unknown Album')
    artist_name = album.get('artist', 'Unknown Artist')
    artist_id = album.get('artistId')
    year = album.get('year', '')
    starred = album.get('starred') is not None
    
    url = build_url({"action": "album", "id": album_id})
    li = xbmcgui.ListItem(label=title)
    
    # Set album info
    li.setInfo("music", {
        "title": title,
        "album": title,
        "artist": artist_name,
        "year": year,
        "mediatype": "album"
    })
    
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
            'Navidrome',
            'Failed to load album',
            xbmcgui.NOTIFICATION_ERROR
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    tracks = album.get('song', [])
    
    for track in tracks:
        add_track_item(api, track)
    
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_TRACKNUM)
    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def add_track_item(api, track):
    """Helper function to add a track list item"""
    track_id = track.get('id')
    
    # Handle both native API and Subsonic API formats
    title = track.get('title') or track.get('name', 'Unknown Track')
    artist = track.get('artist') or track.get('artistName', 'Unknown Artist')
    album_name = track.get('album') or track.get('albumName', 'Unknown Album')
    duration = track.get('duration', 0)
    track_number = track.get('track') or track.get('trackNumber', 0)
    year = track.get('year', '')
    artist_id = track.get('artistId')
    album_id = track.get('albumId')
    starred = track.get('starred') is not None
    
    # For now, use direct HTTP URL (we'll switch to VFS later)
    stream_url = api.get_stream_url(track_id)
    
    li = xbmcgui.ListItem(label=title)
    
    # Set track info
    li.setInfo("music", {
        "title": title,
        "artist": artist,
        "album": album_name,
        "duration": duration,
        "tracknumber": track_number,
        "year": year,
        "mediatype": "song"
    })
    
    # Add cover art - handle both native and Subsonic API
    cover_art = None
    if track.get('coverArt'):
        # Subsonic API format
        cover_art = track.get('coverArt')
    elif track.get('coverArtId'):
        # Alternative Subsonic format
        cover_art = track.get('coverArtId')
    elif track.get('hasCoverArt') and track.get('albumId'):
        # Native API format - use albumId for cover art
        cover_art = track.get('albumId')
    
    if cover_art:
        art_url = api.get_cover_art_url(cover_art)
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
    
    # Add to Playlist
    context_menu.append((
        'Add to Playlist',
        f'RunPlugin({build_url({"action": "add_to_playlist", "id": track_id, "name": title})})'
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
    
    xbmcplugin.addDirectoryItem(
        handle=ADDON_HANDLE,
        url=stream_url,
        listitem=li,
        isFolder=False
    )

def add_load_more_item(action, offset, **extra_params):
    """Add a 'Load More' item for pagination"""
    params = {"action": action, "offset": str(offset)}
    params.update(extra_params)

    url = build_url(params)
    li = xbmcgui.ListItem(label="[Load More...]")
    li.setInfo("music", {"title": "[Load More...]"})

    xbmcplugin.addDirectoryItem(
        handle=ADDON_HANDLE,
        url=url,
        listitem=li,
        isFolder=True
    )


def list_albums_all(offset=0):
    """List all albums with pagination"""
    api = get_api()
    if not api:
        return
    
    # Get items per page from settings
    items_per_page_setting = int(ADDON.getSetting('items_per_page') or '2')
    items_per_page_values = [50, 100, 200, 500, 1000]
    items_per_page = items_per_page_values[items_per_page_setting]
    
    albums = api.get_album_list('alphabeticalByName', size=items_per_page, offset=offset)
    
    if not albums:
        if offset == 0:
            xbmcgui.Dialog().notification(
                'Navidrome',
                'No albums found',
                xbmcgui.NOTIFICATION_INFO
            )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    # Add "Load More" if we got a full page
    if len(albums) >= items_per_page:
        add_load_more_item("albums_all", offset + items_per_page)
    
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ALBUM)
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_albums_random(offset=0):
    """List random albums with pagination"""
    api = get_api()
    if not api:
        return
    
    items_per_page_setting = int(ADDON.getSetting('items_per_page') or '2')
    items_per_page_values = [50, 100, 200, 500, 1000]
    items_per_page = items_per_page_values[items_per_page_setting]
    
    albums = api.get_album_list('random', size=items_per_page, offset=offset)
    
    if not albums:
        if offset == 0:
            xbmcgui.Dialog().notification(
                'Navidrome',
                'No albums found',
                xbmcgui.NOTIFICATION_INFO
            )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    # Add "Load More" if we got a full page
    if len(albums) >= items_per_page:
        add_load_more_item("albums_random", offset + items_per_page)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_albums_recent():
    """List recently added albums"""
    api = get_api()
    if not api:
        return
    
    albums = api.get_album_list('newest', size=50)
    
    if not albums:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'No albums found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_albums_favourites():
    """List favourite albums"""
    api = get_api()
    if not api:
        return
    
    albums = api.get_starred_albums()
    
    if not albums:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'No favourite albums found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_albums_top_rated():
    """List top rated albums"""
    api = get_api()
    if not api:
        return
    
    albums = api.get_album_list('highest', size=50)
    
    if not albums:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'No albums found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_albums_recently_played():
    """List recently played albums"""
    api = get_api()
    if not api:
        return
    
    albums = api.get_album_list('recent', size=50)
    
    if not albums:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'No albums found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_albums_most_played():
    """List most played albums"""
    api = get_api()
    if not api:
        return
    
    albums = api.get_album_list('frequent', size=50)
    
    if not albums:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'No albums found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_songs(offset=0):
    """List all songs with pagination"""
    api = get_api()
    if not api:
        return

    items_per_page_setting = int(ADDON.getSetting('items_per_page') or '2')
    items_per_page_values = [50, 100, 200, 500, 1000]
    items_per_page = items_per_page_values[items_per_page_setting]

    songs = api.get_all_songs(size=items_per_page, offset=offset)

    if not songs:
        if offset == 0:
            xbmcgui.Dialog().notification(
                'Navidrome',
                'No songs found',
                xbmcgui.NOTIFICATION_INFO
            )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    for track in songs:
        add_track_item(api, track)

    # Add "Load More" if we got a full page
    if len(songs) >= items_per_page:
        add_load_more_item("songs", offset + items_per_page)

    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_TITLE)
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ARTIST)
    xbmcplugin.addSortMethod(ADDON_HANDLE, xbmcplugin.SORT_METHOD_ALBUM)
    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_radios():
    """List internet radio stations"""
    api = get_api()
    if not api:
        return
    
    radios = api.get_internet_radios()
    
    if not radios:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'No radio stations found',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for radio in radios:
        radio_id = radio.get('id')
        name = radio.get('name', 'Unknown Radio')
        stream_url = radio.get('streamUrl', '')
        homepage = radio.get('homePageUrl', '')
        
        li = xbmcgui.ListItem(label=name)
        li.setInfo("music", {
            "title": name,
            "comment": homepage,
            "mediatype": "song"
        })
        
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

def play_track(track_id):
    """Play a track"""
    try:
        api = get_api()
        addon = xbmcaddon.Addon()
        
        # Get track details for metadata
        # We need to search for the track or get it from album
        # For now, just create a basic playable item
        
        stream_url = api.get_stream_url(track_id)
        
        # Show transcoding notification if enabled
        if addon.getSettingBool('enable_transcoding'):
            bitrates = [64, 96, 128, 160, 192, 256, 320]
            max_bitrate_index = int(addon.getSetting('max_bitrate') or '4')
            transcode_format = addon.getSetting('transcode_format') or 'mp3'
            bitrate = bitrates[max_bitrate_index]
            
            xbmcgui.Dialog().notification(
                'Navidrome',
                f'Transcoding: {transcode_format.upper()} @ {bitrate} kbps',
                xbmcgui.NOTIFICATION_INFO,
                2000
            )
        
        play_item = xbmcgui.ListItem(path=stream_url)
        xbmcplugin.setResolvedUrl(HANDLE, True, listitem=play_item)
        
    except Exception as e:
        xbmc.log(f"NAVIDROME: Error playing track: {str(e)}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification('Navidrome', 'Error playing track', xbmcgui.NOTIFICATION_ERROR)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def list_playlists():
    """List all playlists"""
    api = get_api()
    if not api:
        return
    
    playlists = api.get_playlists()
    
    if not playlists:
        xbmcgui.Dialog().notification(
            'Navidrome',
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
        
        li.setInfo("music", {
            "title": name,
            "mediatype": "playlist"
        })
        
        # Add cover art if available
        cover_art = playlist.get('coverArt')
        if cover_art:
            art_url = api.get_cover_art_url(cover_art)
            li.setArt({"thumb": art_url})
        
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )
    
    xbmcplugin.setContent(ADDON_HANDLE, 'playlists')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_playlist_tracks(playlist_id):
    """List tracks in a playlist"""
    api = get_api()
    if not api:
        return
    
    playlist = api.get_playlist(playlist_id)
    
    if not playlist:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'Failed to load playlist',
            xbmcgui.NOTIFICATION_ERROR
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    tracks = playlist.get('entry', [])
    
    for track in tracks:
        add_track_item(api, track)
    
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
            'Navidrome',
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
        
        li.setInfo("music", {
            "title": name,
            "artist": name,
            "mediatype": "artist"
        })
        
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
    for track in results.get('song', []):
        add_track_item(api, track)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'mixed')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def star_item(item_id, item_type, item_name):
    """Star an item"""
    api = get_api()
    if not api:
        return
    
    if api.star(item_id, item_type):
        xbmcgui.Dialog().notification(
            'Navidrome',
            f'Starred: {item_name}',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmc.executebuiltin('Container.Refresh')
    else:
        xbmcgui.Dialog().notification(
            'Navidrome',
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
            'Navidrome',
            f'Unstarred: {item_name}',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmc.executebuiltin('Container.Refresh')
    else:
        xbmcgui.Dialog().notification(
            'Navidrome',
            'Failed to unstar item',
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
                    'Navidrome',
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
                    'Navidrome',
                    f'Added to new playlist: {playlist_name}',
                    xbmcgui.NOTIFICATION_INFO
                )
    else:
        # Add to existing playlist
        playlist = playlists[selected - 1]
        playlist_id = playlist.get('id')
        if api.update_playlist(playlist_id, [track_id]):
            xbmcgui.Dialog().notification(
                'Navidrome',
                f'Added to: {playlist.get("name")}',
                xbmcgui.NOTIFICATION_INFO
            )
        else:
            xbmcgui.Dialog().notification(
                'Navidrome',
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
            'Navidrome',
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
        
        li.setInfo("music", {
            "title": genre_name,
            "genre": genre_name
        })
        
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
        li.setInfo("music", {"title": label})
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=url,
            listitem=li,
            isFolder=True
        )
    
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_genre_albums(genre_name):
    """List albums for a genre"""
    api = get_api()
    if not api:
        return
    
    albums = api.get_albums_by_genre(genre_name)
    
    if not albums:
        xbmcgui.Dialog().notification(
            'Navidrome',
            f'No albums found for {genre_name}',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for album in albums:
        add_album_item(api, album)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'albums')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_genre_songs(genre_name):
    """List songs for a genre"""
    api = get_api()
    if not api:
        return
    
    songs = api.get_songs_by_genre(genre_name)
    
    if not songs:
        xbmcgui.Dialog().notification(
            'Navidrome',
            f'No songs found for {genre_name}',
            xbmcgui.NOTIFICATION_INFO
        )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    
    for track in songs:
        add_track_item(api, track)
    
    xbmcplugin.setContent(ADDON_HANDLE, 'songs')
    xbmcplugin.endOfDirectory(ADDON_HANDLE)

def router(paramstring):
    """Route to the appropriate function"""
    params = dict(urllib.parse.parse_qsl(paramstring))
    action = params.get("action")

    if action is None:
        root_menu()
    elif action == "albums_menu":
        albums_menu()
    elif action == "albums_all":
        list_albums_all()
    elif action == "albums_random":
        list_albums_random()
    elif action == "albums_favourites":
        list_albums_favourites()
    elif action == "albums_top_rated":
        list_albums_top_rated()
    elif action == "albums_recent":
        list_albums_recent()
    elif action == "albums_recently_played":
        list_albums_recently_played()
    elif action == "albums_most_played":
        list_albums_most_played()
    elif action == "artists":
        list_artists()
    elif action == "artist":
        list_artist_albums(params.get("id"))
    elif action == "album":
        list_album_tracks(params.get("id"))
    elif action == "songs":
        list_songs()
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
    elif action == "add_to_playlist":
        add_to_playlist_dialog(params.get("id"), params.get("name"))
    elif action == "genres":
        list_genres()
    elif action == "genre":
        list_genre_content(params.get("name"))
    elif action == "genre_albums":
        list_genre_albums(params.get("name"))
    elif action == "genre_songs":
        list_genre_songs(params.get("name"))
    elif action == "albums_all":
        list_albums_all(int(params.get("offset", 0)))
    elif action == "albums_random":
        list_albums_random(int(params.get("offset", 0)))
    elif action == "songs":
        list_songs(int(params.get("offset", 0)))
    else:
        xbmc.log(f"Unknown action: {action}", xbmc.LOGWARNING)
        root_menu()


if __name__ == "__main__":
    router(sys.argv[2][1:])