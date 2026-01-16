# Navidrome Kodi Plugin

**plugin.kodi.navidrome** is a Kodi addon that allows you to stream your music collection directly from a [Navidrome](https://www.navidrome.org/) server. It provides a seamless integration with Kodi's music interface, supporting browsing, streaming, and library management features.

## Features

*   **Music Streaming**: Stream your entire music library from Navidrome to Kodi.
*   **Browsing**:
    *   **Albums**: Browse by All, Random, Favourites, Top Rated, Recently Added, Recently Played, and Most Played.
    *   **Artists**: Browse all artists with cover art.
    *   **Songs**: Browse individual tracks.
    *   **Genres**: Browse albums and songs by genre.
    *   **Playlists**: Access and play your existing Navidrome playlists.
    *   **Radios**: Listen to internet radio stations configured in Navidrome.
*   **Search**: Search for Artists, Albums, and Songs.
*   **Library Management**:
    *   **Star/Unstar**: Mark albums and songs as favourites directly from Kodi.
    *   **Playlists**: Create new playlists or add tracks to existing ones.
*   **Scrobbling**: Fully functional scrobbling and "Now Playing" status updates to your Navidrome server.
*   **Transcoding**: Supports server-side transcoding with configurable bitrates and formats (MP3, etc.) for bandwidth management.

## Installation

1.  Download the latest release zip file from the [Releases](https://github.com/colinfredynand/plugin.kodi.navidrome/releases) page.
2.  Open Kodi and go to **Settings** > **Add-ons**.
3.  Select **Install from zip file**.
4.  Navigate to the downloaded zip file and select it.
5.  Wait for the "Add-on enabled" notification.

## Configuration

After installation, you must configure the addon to connect to your Navidrome server:

1.  Go to **Add-ons** > **Music add-ons**.
2.  Right-click (or long-press) on **Navidrome** and select **Settings**.
3.  Enter your connection details:
    *   **Server URL**: The full URL to your Navidrome instance (e.g., `https://music.mydomain.com` or `http://192.168.1.10:4533`).
    *   **Username**: Your Navidrome username.
    *   **Password**: Your Navidrome password.
4.  (Optional) Configure **Transcoding**:
    *   **Enable Transcoding**: Toggle on/off.
    *   **Max Bitrate**: Select your preferred quality (e.g., 320 kbps, 128 kbps).
    *   **Format**: Choose the transcoding format (default: mp3).

## Known Issues & Roadmap

*   **VFS Implementation**: The addon currently uses direct HTTP URLs for streaming. Full implementation of the Kodi Virtual File System (VFS) path is planned. This will improve compatibility and functionality with certain Kodi features that rely on local-like file access.
    *   *Current Status*: Functional for streaming and playback.

## Development

This addon is written in Python and uses the standard Kodi Addon API.

### Structure
*   `default.py`: Main addon entry point and routing logic.
*   `service.py`: Background service (likely handles scrobbling/status updates).
*   `lib/navidrome_api.py`: Wrapper for the Navidrome/Subsonic API.
*   `resources/`: Settings, language files, and images.

## License

This project is licensed under the [GPL-3.0 License](LICENSE).

## Acknowledgments

*   Thanks to the [Navidrome](https://www.navidrome.org/) team for the excellent music server.
*   Developed by [colinfredynand](https://github.com/colinfredynand).
