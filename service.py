import xbmc
import xbmcaddon
import time

from lib.navidrome_api import NavidromeAPI


class NavidromeMonitor(xbmc.Monitor):
    """Monitor for Kodi events"""
    
    def __init__(self, service):
        super().__init__()
        self.service = service
    
    def onSettingsChanged(self):
        """Called when addon settings are changed"""
        xbmc.log("NAVIDROME SERVICE: Settings changed, reinitializing API", xbmc.LOGINFO)
        self.service.init_api()


class NavidromePlayer(xbmc.Player):
    """Player monitor for tracking playback"""
    
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.current_track_id = None
        self.play_start_time = None
        self.track_duration = 0
        self.scrobbled = False
    
    def onAVStarted(self):
        """Called when playback starts"""
        if not self.isPlayingAudio():
            return
        
        # Get track info
        track_id = self._get_navidrome_track_id()
        
        if not track_id:
            return
        
        xbmc.log(f"NAVIDROME SERVICE: Started playing track {track_id}", xbmc.LOGINFO)
        
        self.current_track_id = track_id
        self.play_start_time = time.time()
        self.scrobbled = False
        
        # Get track duration
        try:
            self.track_duration = self.getTotalTime()
        except:
            self.track_duration = 0
        
        # Update now playing if enabled
        if self.service.addon.getSettingBool('enable_now_playing'):
            self.service.update_now_playing(track_id)
    
    def onPlayBackStopped(self):
        """Called when playback stops"""
        self._handle_playback_end()
    
    def onPlayBackEnded(self):
        """Called when playback ends naturally"""
        self._handle_playback_end()
    
    def onPlayBackPaused(self):
        """Called when playback is paused"""
        xbmc.log("NAVIDROME SERVICE: Playback paused", xbmc.LOGDEBUG)
    
    def onPlayBackResumed(self):
        """Called when playback resumes"""
        xbmc.log("NAVIDROME SERVICE: Playback resumed", xbmc.LOGDEBUG)
    
    def _handle_playback_end(self):
        """Handle end of playback"""
        if not self.current_track_id:
            return
        
        # Check if scrobbling is enabled
        if not self.service.addon.getSettingBool('enable_scrobbling'):
            self.current_track_id = None
            self.play_start_time = None
            return
        
        # Check if we should scrobble
        if not self.scrobbled and self.play_start_time:
            play_time = time.time() - self.play_start_time
            
            # Get scrobble threshold from settings
            scrobble_threshold = self.service.addon.getSettingInt('scrobble_threshold') or 50
            threshold_seconds = (self.track_duration * scrobble_threshold / 100) if self.track_duration > 0 else 240
            
            # Scrobble if played past threshold or at least 4 minutes
            should_scrobble = play_time >= min(threshold_seconds, 240)
            
            if should_scrobble:
                xbmc.log(f"NAVIDROME SERVICE: Scrobbling track {self.current_track_id}", xbmc.LOGINFO)
                self.service.scrobble(self.current_track_id)
                self.scrobbled = True
        
        self.current_track_id = None
        self.play_start_time = None
    
    def _get_navidrome_track_id(self):
        """Extract Navidrome track ID from the playing URL"""
        try:
            playing_file = self.getPlayingFile()
            
            # Check if it's a Navidrome URL
            if 'rest/stream' not in playing_file:
                return None
            
            # Extract ID from URL parameter
            import urllib.parse
            parsed = urllib.parse.urlparse(playing_file)
            params = urllib.parse.parse_qs(parsed.query)
            
            if 'id' in params:
                return params['id'][0]
            
            return None
        except Exception as e:
            xbmc.log(f"NAVIDROME SERVICE: Error getting track ID: {str(e)}", xbmc.LOGERROR)
            return None
    
    def check_scrobble_progress(self):
        """Check if we should scrobble based on playback progress"""
        if not self.current_track_id or self.scrobbled:
            return
        
        # Check if scrobbling is enabled
        if not self.service.addon.getSettingBool('enable_scrobbling'):
            return
        
        if not self.isPlayingAudio():
            return
        
        if not self.play_start_time:
            return
        
        play_time = time.time() - self.play_start_time
        
        # Get scrobble threshold from settings
        scrobble_threshold = self.service.addon.getSettingInt('scrobble_threshold') or 50
        threshold_seconds = (self.track_duration * scrobble_threshold / 100) if self.track_duration > 0 else 240
        
        # Scrobble if played past threshold or at least 4 minutes
        should_scrobble = play_time >= min(threshold_seconds, 240)
        
        if should_scrobble:
            xbmc.log(f"NAVIDROME SERVICE: Scrobbling track {self.current_track_id} (progress)", xbmc.LOGINFO)
            self.service.scrobble(self.current_track_id)
            self.scrobbled = True


class NavidromeService:
    """Main service class"""
    
    def __init__(self):
        self.addon = xbmcaddon.Addon('plugin.kodi.navidrome')
        self.api = None
        self.monitor = NavidromeMonitor(self)
        self.player = NavidromePlayer(self)
        
        xbmc.log("NAVIDROME SERVICE: Starting", xbmc.LOGINFO)
        self.init_api()
    
    def init_api(self):
        """Initialize API connection"""
        try:
            server_url = self.addon.getSetting('server_url')
            username = self.addon.getSetting('username')
            password = self.addon.getSetting('password')
            
            if server_url and username and password:
                self.api = NavidromeAPI(server_url, username, password)
                xbmc.log("NAVIDROME SERVICE: API initialized", xbmc.LOGINFO)
            else:
                self.api = None
                xbmc.log("NAVIDROME SERVICE: No credentials configured", xbmc.LOGWARNING)
        except Exception as e:
            xbmc.log(f"NAVIDROME SERVICE: Error initializing API: {str(e)}", xbmc.LOGERROR)
            self.api = None
    
    def update_now_playing(self, track_id):
        """Update now playing status"""
        if not self.api:
            return
        
        try:
            self.api.update_now_playing(track_id)
        except Exception as e:
            xbmc.log(f"NAVIDROME SERVICE: Error updating now playing: {str(e)}", xbmc.LOGERROR)
    
    def scrobble(self, track_id):
        """Scrobble a track"""
        if not self.api:
            return
        
        try:
            self.api.scrobble(track_id)
        except Exception as e:
            xbmc.log(f"NAVIDROME SERVICE: Error scrobbling: {str(e)}", xbmc.LOGERROR)
    
    def run(self):
        """Main service loop"""
        xbmc.log("NAVIDROME SERVICE: Running", xbmc.LOGINFO)
        
        # Check every 10 seconds for scrobble progress
        while not self.monitor.abortRequested():
            if self.monitor.waitForAbort(10):
                break
            
            # Check if we should scrobble based on progress
            self.player.check_scrobble_progress()
        
        xbmc.log("NAVIDROME SERVICE: Stopped", xbmc.LOGINFO)


if __name__ == '__main__':
    service = NavidromeService()
    service.run()