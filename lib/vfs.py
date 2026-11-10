"""
Kodi Virtual File System (xbmcvfs) layer for the Navidrome addon.

Everything the addon writes to disk goes through xbmcvfs rather than Python's
``open()``/``os`` so it works on every platform Kodi runs on (Android, UWP,
Libreelec, network shares, ...) where the addon data directory is not
necessarily a plain local path.

See https://alwinesch.github.io/group__python__xbmcvfs.html

Provides:
  * path helpers rooted at special://profile/addon_data/<addon id>/
  * JSON/text/binary read + write helpers built on xbmcvfs.File
  * MediaCache — offline storage of streamed tracks, with metadata sidecars,
    size accounting via xbmcvfs.Stat and LRU pruning.
"""

import json
import time

import xbmc
import xbmcaddon
import xbmcvfs

ADDON_ID = 'plugin.kodi.navidrome'

# Subdirectories under the addon profile
DIR_MEDIA = 'media'

# Well known files
FILE_SESSION = 'session.json'
FILE_NOW_PLAYING = 'nowplaying.json'

_SAFE_CHARS = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.')


def _log(message, level=xbmc.LOGDEBUG):
    xbmc.log(f"NAVIDROME VFS: {message}", level)


# Paths
def profile_dir():
    """Return the addon profile directory as a special:// path (with trailing /)."""
    try:
        path = xbmcaddon.Addon(ADDON_ID).getAddonInfo('profile')
    except Exception:
        path = f'special://profile/addon_data/{ADDON_ID}/'
    if not path.endswith('/'):
        path += '/'
    return path


def join(*parts):
    """Join path fragments with '/' — special:// paths always use forward slashes."""
    cleaned = [str(parts[0]).rstrip('/')]
    cleaned.extend(str(p).strip('/') for p in parts[1:] if str(p).strip('/'))
    return '/'.join(cleaned)


def profile_path(*parts):
    """Build a path inside the addon profile directory."""
    return join(profile_dir().rstrip('/'), *parts)


def safe_name(name):
    """Reduce an arbitrary id/name to something safe on every filesystem."""
    text = ''.join(c if c in _SAFE_CHARS else '_' for c in str(name))
    text = text.strip('.') or 'item'
    # Not xbmcvfs.makeLegalFilename: it treats its input as a folder and
    # appends '/', and the characters left here are legal everywhere anyway.
    return text[:96]


# Thin xbmcvfs wrappers
def exists(path):
    try:
        return xbmcvfs.exists(path)
    except Exception as exc:
        _log(f"exists({path}) failed: {exc}", xbmc.LOGWARNING)
        return False


def dir_exists(path):
    # xbmcvfs.exists() only recognises a directory when the path ends in '/'.
    return exists(path.rstrip('/') + '/')


def ensure_dir(path):
    """Create a directory and every missing parent. Returns True when usable."""
    if dir_exists(path):
        return True
    try:
        xbmcvfs.mkdirs(path)
    except Exception as exc:
        _log(f"mkdirs({path}) failed: {exc}", xbmc.LOGWARNING)
    return dir_exists(path)


def delete(path):
    try:
        return xbmcvfs.delete(path)
    except Exception as exc:
        _log(f"delete({path}) failed: {exc}", xbmc.LOGWARNING)
        return False


def listdir(path):
    """Return (dirs, files); empty lists when the folder is missing."""
    if not dir_exists(path):
        return [], []
    try:
        dirs, files = xbmcvfs.listdir(path)
        return list(dirs), list(files)
    except Exception as exc:
        _log(f"listdir({path}) failed: {exc}", xbmc.LOGWARNING)
        return [], []


def stat(path):
    """Return an xbmcvfs.Stat for path, or None."""
    if not exists(path):
        return None
    try:
        return xbmcvfs.Stat(path)
    except Exception as exc:
        _log(f"Stat({path}) failed: {exc}", xbmc.LOGWARNING)
        return None


def file_size(path):
    st = stat(path)
    try:
        return int(st.st_size()) if st else 0
    except Exception:
        return 0


def file_mtime(path):
    st = stat(path)
    try:
        return float(st.st_mtime()) if st else 0.0
    except Exception:
        return 0.0


def translate(path):
    """Resolve a special:// path to a real filesystem path."""
    try:
        return xbmcvfs.translatePath(path)
    except Exception:
        return path


# Read/write helpers
def read_bytes(path):
    if not exists(path):
        return None
    handle = None
    try:
        handle = xbmcvfs.File(path)
        return bytes(handle.readBytes())
    except Exception as exc:
        _log(f"read_bytes({path}) failed: {exc}", xbmc.LOGWARNING)
        return None
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def write_bytes(path, data):
    parent = path.rsplit('/', 1)[0]
    if not ensure_dir(parent):
        return False
    handle = None
    try:
        handle = xbmcvfs.File(path, 'w')
        handle.write(bytearray(data))
        return True
    except Exception as exc:
        _log(f"write_bytes({path}) failed: {exc}", xbmc.LOGWARNING)
        return False
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def read_json(path, max_age=None):
    """Read a JSON document. Returns None when missing, unreadable or stale."""
    raw = read_bytes(path)
    if raw is None:
        return None
    if max_age is not None:
        age = time.time() - file_mtime(path)
        if age > max_age:
            return None
    try:
        return json.loads(raw.decode('utf-8'))
    except Exception as exc:
        _log(f"read_json({path}) failed: {exc}", xbmc.LOGWARNING)
        return None


def write_json(path, obj):
    try:
        payload = json.dumps(obj).encode('utf-8')
    except Exception as exc:
        _log(f"write_json({path}) encode failed: {exc}", xbmc.LOGWARNING)
        return False
    return write_bytes(path, payload)


# Session (auth) cache
def load_session(key, max_age):
    """Return the cached session dict when it matches key and is fresh."""
    data = read_json(profile_path(FILE_SESSION), max_age=max_age)
    if isinstance(data, dict) and data.get('key') == key:
        return data
    return None


def save_session(key, payload):
    data = dict(payload)
    data['key'] = key
    return write_json(profile_path(FILE_SESSION), data)


def clear_session():
    return delete(profile_path(FILE_SESSION))


# Now playing marker — lets the service identify locally cached playback
def set_now_playing(track_id, path):
    return write_json(profile_path(FILE_NOW_PLAYING), {
        'id': track_id,
        'path': path,
        'ts': time.time(),
    })


def get_now_playing(max_age=86400):
    return read_json(profile_path(FILE_NOW_PLAYING), max_age=max_age)


class MediaCache:
    """
    Offline copies of streamed tracks, stored under the addon profile.

    Each cached track is two files:
      <media>/<id>.<suffix>   the audio, playable straight from Kodi
      <media>/<id>.json       a metadata sidecar used to browse the cache
    """

    def __init__(self, max_bytes=0):
        self.root = profile_path(DIR_MEDIA)
        self.max_bytes = max(0, int(max_bytes or 0))

    def _sidecar(self, track_id):
        return join(self.root, f'{safe_name(track_id)}.json')

    def media_path(self, track_id, suffix):
        suffix = safe_name(suffix or 'bin').lstrip('.') or 'bin'
        return join(self.root, f'{safe_name(track_id)}.{suffix}')

    def get(self, track_id):
        """Return the playable path of a cached track, or None."""
        if not track_id:
            return None
        meta = read_json(self._sidecar(track_id))
        if not meta:
            return None
        path = meta.get('path')
        if path and exists(path):
            return path
        # Sidecar without media — clean it up so the state stays consistent
        delete(self._sidecar(track_id))
        return None

    def has(self, track_id):
        return self.get(track_id) is not None

    def put(self, track_id, suffix, data, metadata=None):
        """Store already downloaded bytes. Returns the path or None."""
        path = self.media_path(track_id, suffix)
        if not write_bytes(path, data):
            return None
        return self._write_sidecar(track_id, path, metadata)

    def open_writer(self, track_id, suffix):
        """
        Open an xbmcvfs.File for streaming a download in chunks.
        Returns (handle, path) or (None, None).
        """
        path = self.media_path(track_id, suffix)
        if not ensure_dir(self.root):
            return None, None
        try:
            return xbmcvfs.File(path, 'w'), path
        except Exception as exc:
            _log(f"open_writer({path}) failed: {exc}", xbmc.LOGWARNING)
            return None, None

    def commit(self, track_id, path, metadata=None):
        """Finish a chunked download: write the sidecar and prune."""
        if not exists(path) or file_size(path) <= 0:
            delete(path)
            return None
        result = self._write_sidecar(track_id, path, metadata)
        self.prune()
        return result

    def _write_sidecar(self, track_id, path, metadata):
        meta = dict(metadata or {})
        meta.update({
            'id': track_id,
            'path': path,
            'size': file_size(path),
            'cached_at': time.time(),
        })
        if not write_json(self._sidecar(track_id), meta):
            delete(path)
            return None
        return path

    def remove(self, track_id):
        meta = read_json(self._sidecar(track_id)) or {}
        path = meta.get('path')
        if path:
            delete(path)
        delete(self._sidecar(track_id))
        return True

    def entries(self):
        """Every cached track's metadata, newest first."""
        _dirs, files = listdir(self.root)
        items = []
        for name in files:
            if not name.endswith('.json'):
                continue
            meta = read_json(join(self.root, name))
            if not isinstance(meta, dict) or not meta.get('path'):
                continue
            if not exists(meta['path']):
                continue
            items.append(meta)
        items.sort(key=lambda m: m.get('cached_at', 0), reverse=True)
        return items

    def total_size(self):
        _dirs, files = listdir(self.root)
        return sum(file_size(join(self.root, name)) for name in files)

    def prune(self):
        """Drop the least recently cached tracks until back under the limit."""
        if not self.max_bytes:
            return 0
        total = self.total_size()
        if total <= self.max_bytes:
            return 0
        removed = 0
        for meta in sorted(self.entries(), key=lambda m: m.get('cached_at', 0)):
            if total <= self.max_bytes:
                break
            total -= int(meta.get('size', 0) or 0)
            self.remove(meta.get('id'))
            removed += 1
        _log(f"pruned {removed} cached tracks", xbmc.LOGINFO)
        return removed

    def clear(self):
        _dirs, files = listdir(self.root)
        for name in files:
            delete(join(self.root, name))
        return len(files)
