#!/usr/bin/env python3
# fully async example program to monitor a folder and upload/display on Frame TV
# NOTE: install Pillow (pip install Pillow) to automatically syncronize art on TV wth uploaded_files.json.

'''
This program will read the files in a designated folder (with allowed extensions) and upload them to your TV. It keeps track of which files correspond to what
content_id on your TV by saving the data in a file called uploaded_files.json. it also keeps track of when the selected artwork was last changed.

It monitors the folder for changes every check seconds (5 by default), new files are uploaded to the TV, removed files are deleted from the TV, and if a file
is changed, the old content is removed from the TV and the new content uploaded to the TV. Content is only changed if the TV is in art mode.

if check is set to 0 seconds, the program will run once and exit. You can then run it periodically (say with a cron job).

if there is more than one file in the folder, the current artword displayed is changed every update minutes (0) by default (which means do not select any artwork),
otherwise the single file in the folder is selected to be displayed. this also only happens when the TV is in art mode.

If you have PIL installed, the initial syncronization is automatic, the first time the program is run.

If the on (-O) option is selected, the program wil exit if the TV is not on (TV or art mode).
If the sequential (-S) option is selected, then the slideshow is sequential, not random (random is the default)
The default checking period is 60 seconds or the update period whichever is less.

Example:
    1) Your TV is used to display one image, that changes every day, you have a program that grabs the image and puts it in a folder. The image always has the same name.
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path> -c 0
       to update the image on the Tv after the script that grabs the file runs
       If you are unsure if the TV will be on when you run the program
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path> -c 0 -O
       or
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path> -c 60
       and leave it running
       
    2) You use your TV to display your own artwork, you want a slideshow that displays a random artwork every minute, but want to add/remove art from a network share
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path_to_share> -u 1
       and leave it running. Add/remove art from the network share folder to include it/remove it from the slideshow.
       If you want an update every 15 seconds
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path_to_share> -u 0.25
       
    3) you have artwork on the TV marked as "favourites", but want to inclue your own artwork from a folder in a random slideshow that updates once a day
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path> -c 3600 -u 1440 -F
       and leave it running. Add/remove art from the folder to include it/remove it from the slideshow.
       
    4) You have some standard art uploaded to your TV, that you slideshow from the TV, but want to add seasonal artworks to the slideshow that you change from time to time.
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path> -c 3600
       and leave it running. Add/remove art from the folder to include it/remove it from the slideshow.
       or
       run ./async_art_update_from_directory.py <tv_ip> -f <folder_path> -c 0 -O
       after updating the files in the folder
'''

import sys
import logging
import os
import socket
import uuid
import re
import io
import random
import json
import asyncio
import time
import datetime
import argparse
import csv
import unicodedata
from signal import SIGTERM, SIGINT
HAVE_PIL = False
try:
    # Import Pillow submodules defensively. In some runtime environments a
    # partially-initialised PIL package may exist and lack attributes like
    # ImageFilter; catch broad exceptions to avoid aborting module import.
    from PIL import Image
    # ImageFilter and ImageChops are optional helpers; import if available
    try:
        from PIL import ImageFilter, ImageChops  # type: ignore
    except Exception:
        ImageFilter = None
        ImageChops = None
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

# from samsungtvws.async_art import SamsungTVAsyncArt  # Moved inside class
# from samsungtvws import __version__  # Not used

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    mqtt = None

logging.basicConfig(level=logging.INFO)


def parseargs():
    # Add command line argument parsing
    parser = argparse.ArgumentParser(description='Async Upload images to Samsung TV')
    parser.add_argument('ip', action="store", type=str, default=None, help='ip address of TV (default: %(default)s))')
    parser.add_argument('-f','--folder', action="store", type=str, default="./images", help='folder to load images from (default: %(default)s))')
    parser.add_argument('-m','--matte', action="store", type=str, default="none", help='default matte to use (default: %(default)s))')
    parser.add_argument('-t','--token_file', action="store", type=str, default="token_file.txt", help='default token file to use (default: %(default)s))')
    parser.add_argument('-u','--update', action="store", type=float, default=0, help='slideshow update period (mins) 0=off (default: %(default)s))')
    parser.add_argument('-c','--check', action="store", type=int, default=60, help='how often to check for new art 0=run once (default: %(default)s))')
    parser.add_argument('-s','--sync', action='store_false', default=True, help='automatically syncronize (needs Pil library) (default: %(default)s))')
    parser.add_argument('-S','--sequential', action='store_true', default=False, help='sequential slide show (default: %(default)s))')
    parser.add_argument('-O','--on', action='store_true', default=False, help='exit if TV is off (default: %(default)s))')
    parser.add_argument('-F','--favourite', action='store_true', default=False, help='include favourites in rotation (default: %(default)s))')
    parser.add_argument('-D','--debug', action='store_true', default=False, help='Debug mode (default: %(default)s))')
    parser.add_argument('-e','--exclude', action="store", type=str, nargs='*', default=[], help='filenames to exclude from slideshow (default: %(default)s))')
    parser.add_argument('-E','--exclude-content-ids', action="store", type=str, nargs='*', default=[], help='content_ids to exclude from slideshow (default: %(default)s))')
    parser.add_argument('--standby', action="store", type=str, default=None, help='filename to select as standby before starting slideshow (default: %(default)s))')
    # MQTT discovery is now the default integration path; no HA REST args
    return parser.parse_args()
    
class PIL_methods:
    
    def __init__(self, mon):
        self.log = logging.getLogger('Main.'+__class__.__name__)
        self.mon = mon
        self.folder = self.mon.folder
        self.uploaded_files = self.mon.uploaded_files
        
    async def initialize(self):
        '''
        initialize uploaded_files using PIL
        compares the file data with thumbnails to find the content_id and write to uploaded_files
        if it doesn't already exist
        '''
        if not HAVE_PIL:
            return
        self.log.info('Checking uploaded files list using PIL')
        files_images = self.load_files()
        if files_images:
            self.log.info('getting My Photos list')
            my_photos = await self.mon.get_tv_content('MY-C0002')
            if my_photos is not None and len(my_photos) > 0:
                await self.check_thumbnails(files_images, my_photos)
            else:
                self.log.info('no photos found on tv')
        else:
            self.log.info('no files, using origional uploaded files list')
            
    async def check_thumbnails(self, files_images, my_photos):
        '''
        download thumbnails from my_photos to compare with file data
        save any updates
        '''
        self.log.info('downloading My Photos thumbnails')
        my_photos_thumbnails = await self.get_thumbnails(my_photos)
        if my_photos_thumbnails:
            self.log.info('checking thumbnails against {} files, please wait...'.format(len(files_images)))
            self.compare_thumbnails(files_images, my_photos_thumbnails)
            self.mon.write_program_data()
        else:
            self.log.info('failed to get thumbnails')
            
    def compare_thumbnails(self, files_images, my_photos_thumbnails):
        '''
        compare file data with thumbnails to find a match, and update update_uploaded_files
        '''
        for k, (filename, file_data) in enumerate(files_images.items()):
            for i, (my_content_id, my_data) in enumerate(my_photos_thumbnails.items()):
                self.log_progress(len(files_images)*len(my_photos_thumbnails), k*len(files_images)+i)
                self.log.debug('checking: {} against {}, thumbnail: {} bytes'.format(filename, my_content_id, len(my_data)))
                if self.are_images_equal(Image.open(io.BytesIO(my_data)), file_data):
                    self.log.info('found uploaded file: {} as {}'.format(filename, my_content_id))
                    if filename not in self.uploaded_files.keys():
                        self.mon.update_uploaded_files(filename, my_content_id)
                    break
        
    def log_progress(self, total, count):
        '''
        log % progress every 10% if this will take a while
        '''
        if total >= 1000:
            percent = min(100,(count*100)//total)
            if count % (total//10) == 0:
                self.log.info('{}% complete'.format(percent))
        
    def load_files(self):
        '''
        reads folder files, and returns dictionary of filenames and binary data
        only used if PIL is installed
        '''
        files = self.mon.get_folder_files()
        self.log.info('loading files: {}'.format(files))
        files_images = self.get_files_dict(files)
        self.log.info('loaded: {}'.format(list(files_images.keys())))
        return files_images
        
    def get_files_dict(self, files):
        '''
        makes a dictionary of filename and file binary data
        warns if file type given by extension is wrong
        only used if PIL is installed
        '''
        files_images = {}
        for file in files:
            # Hard-skip any non-image artifacts like CSVs
            try:
                ext = os.path.splitext(file)[1].lower()
            except Exception:
                ext = ''
            if ext in ['.csv', '.json', '.jsonl', '.txt']:
                continue
            try:
                data = Image.open(os.path.join(self.folder, file))
                format = self.mon.get_file_type(os.path.join(self.folder, file), data)
                if not (file.lower().endswith(format) or (format=='jpeg' and file.lower().endswith('jpg'))):
                    self.log.warning('file: {} is of type {}, the extension is wrong! please fix this'.format(file, format))
                files_images[file] = data
            except Exception as e:
                self.log.warning('Error loading: {}, {}'.format(file, e))
        return files_images
        
    async def get_thumbnails(self, content_ids):
        '''
        gets thumbnails from tv in list of content_ids
        returns dictionary of content_ids and binary data
        only used if PIL is installed
        '''
        thumbnails = {}
        if content_ids:
            if self.mon.api_version == 0:
                thumbnails = {content_id:await self.mon.tv.get_thumbnail(content_id) for content_id in content_ids}
            elif self.mon.api_version == 1:
                thumbnails = {os.path.splitext(k)[0]:v for k,v in (await self.mon.tv.get_thumbnail_list(content_ids)).items()}
        self.log.info('got {} thumbnails'.format(len(thumbnails)))
        return thumbnails
        
    def fix_file_type(self, filename, file_type, image_data=None):
        if not all([HAVE_PIL, file_type]):
            return file_type
        org = file_type
        file_type = Image.open(filename).format.lower() if not image_data else image_data.format.lower()
        if file_type in['jpg', 'jpeg', 'mpo']:
            file_type = 'jpeg'
        if not (org == file_type or (org == 'jpg' and file_type == 'jpeg')):
            self.log.warning('file {} type changed from {} to {}'.format(filename, org, file_type))
        return file_type
        
    def are_images_equal(self, img1, img2):
        '''
        rough check if images are similar using PIL (avoid numpy which is faster)
        '''
        img1 = img1.convert('L').resize((384, 216)).filter(ImageFilter.GaussianBlur(radius=2))
        img2 = img2.convert('L').resize((384, 216)).filter(ImageFilter.GaussianBlur(radius=2))
        img3 = ImageChops.difference(img1, img2)    #updated 11/3/25 per suggestion in issue #11
        diff = sum(list(img3.getdata()))/(384*216)  #normalize
        equal_content = diff <= 1.0                 #pick a threshhold
        self.log.debug('equal_content: {}, diff: {}'.format(equal_content, diff))
        return equal_content
    
class monitor_and_display:
    
    allowed_ext = ['jpg', 'jpeg', 'png', 'bmp', 'tif']
    
    def __init__(self, ip, folder, period=5, update_time=1440, include_fav=False, sync=True, matte='none', sequential=False, on=False, token_file=None, exclude=[], exclude_content_ids=[], standby=None):
        self.log = logging.getLogger('Main.'+__class__.__name__)
        self.debug = self.log.getEffectiveLevel() <= logging.DEBUG
        self.ip = ip
        self.folder = folder
        self.media_root = os.environ.get('SAMSUNG_TV_ART_MEDIA_ROOT', folder)
        self.cache_path = os.environ.get('SAMSUNG_TV_ART_CACHE_FILE', '/data/uploaded_files_cache.json')
        self.current_key = None
        self.cache = {}
        self.selection_mtime = None
        self.selected_collections = []  # List of selected collection folders for multi-select mode
        # MQTT-driven selection (optional)
        # Always drive selections from retained MQTT; do not allow disabling via env
        self.selection_from_mqtt = True
        self.selection_mqtt_topic = os.environ.get('SAMSUNG_TV_ART_SELECTION_MQTT_TOPIC', 'frame_tv/selected_collections/state')
        self._pending_selection_change = False
        self.selection_only = os.environ.get('SAMSUNG_TV_ART_SELECTION_ONLY', '').lower() in ['1', 'true', 'yes']
        self.artmode_refresh_seconds = int(os.environ.get('SAMSUNG_TV_ART_MODE_CHECK_SECONDS', '120'))
        self.last_artmode_check = 0
        self.consecutive_failures = 0
        self.max_backoff_seconds = 1800  # Max 30 minutes between retries
        self.reconnect_delay = 5
        self.update_time = int(max(0, update_time*60))   #convert minutes to seconds
        self.period = min(max(5, period), self.update_time) if self.update_time > 0 else period
        self.include_fav = include_fav
        self.sync = sync
        self.matte = matte
        self.sequential = sequential
        self.on = on
        self.exclude = exclude
        self.exclude_content_ids = exclude_content_ids
        self.standby = standby
        self.standby_content_id = None
        # Autosave token to file
        self.token_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), token_file) if token_file else token_file
        self.program_data_path = './uploaded_files.json'
        self.uploaded_files = {}
        self.fav = set()
        self.api_version = 0
        self.start = time.time()
        self.current_content_id = None
        self.shown_content_ids = set()  # Track shown images for shuffle-without-repeat
        self.pil = PIL_methods(self)
        self.tv = None  # Defer TV connection until start_monitoring
        # Rate limits (configurable)
        self.upload_delay_seconds = int(os.environ.get('SAMSUNG_TV_ART_UPLOAD_DELAY_SECONDS', '15'))
        self.delete_delay_seconds = int(os.environ.get('SAMSUNG_TV_ART_DELETE_DELAY_SECONDS', '5'))
        # MQTT configuration (optional, for HA MQTT Discovery)
        self.mqtt_enabled = os.environ.get('SAMSUNG_TV_ART_MQTT_DISCOVERY', 'false').lower() in ['1','true','yes']
        self.mqtt_host = os.environ.get('SAMSUNG_TV_ART_MQTT_HOST', 'mosquitto')
        self.mqtt_port = int(os.environ.get('SAMSUNG_TV_ART_MQTT_PORT', '1883'))
        self.mqtt_username = os.environ.get('SAMSUNG_TV_ART_MQTT_USERNAME')
        self.mqtt_password = os.environ.get('SAMSUNG_TV_ART_MQTT_PASSWORD')
        self.mqtt_discovery_prefix = os.environ.get('SAMSUNG_TV_ART_MQTT_DISCOVERY_PREFIX', 'homeassistant')
        self.mqtt_state_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_STATE_TOPIC', 'frame_tv/selected_artwork/state')
        self.mqtt_attr_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_ATTR_TOPIC', 'frame_tv/selected_artwork/attributes')
        self.mqtt_unique_id = os.environ.get('SAMSUNG_TV_ART_MQTT_UNIQUE_ID', 'frame_tv_art_selected_artwork')
        # MQTT command/ack topics
        self.mqtt_cmd_prefix = os.environ.get('SAMSUNG_TV_ART_MQTT_CMD_PREFIX', 'frame_tv/cmd')
        self.mqtt_ack_prefix = os.environ.get('SAMSUNG_TV_ART_MQTT_ACK_PREFIX', 'frame_tv/ack')
        # Collections sensor topics
        self.mqtt_collections_state_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_COLLECTIONS_STATE', 'frame_tv/collections/state')
        self.mqtt_collections_attr_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_COLLECTIONS_ATTR', 'frame_tv/collections/attributes')
        self.mqtt_collections_unique_id = os.environ.get('SAMSUNG_TV_ART_MQTT_COLLECTIONS_UNIQUE_ID', 'frame_tv_art_collections')
        self.mqtt_selected_collections_state_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SELECTED_COLLECTIONS_STATE', 'frame_tv/selected_collections/summary')
        self.mqtt_selected_collections_attr_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SELECTED_COLLECTIONS_ATTR', 'frame_tv/selected_collections/attributes')
        # Settings topics (MQTT-only settings management)
        self.mqtt_settings_state_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SETTINGS_STATE', 'frame_tv/settings/state')
        self.mqtt_settings_attr_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SETTINGS_ATTR', 'frame_tv/settings/attributes')
        self.ha_rest_enabled = False  # REST disabled in MQTT-only build
        self._mqtt = None
        self._mqtt_config_published = False
        # CSV metadata (optional)
        self.csv_path = os.environ.get('SAMSUNG_TV_ART_CSV_PATH', '/app/artwork_data.csv')
        self._csv_headers = []
        self._csv_by_file = {}
        # Collections source (folders by default; optional unique artists from CSV)
        self.collections_from_csv = os.environ.get('SAMSUNG_TV_ART_COLLECTIONS_FROM_CSV', 'true').lower() in ['1','true','yes']
        # CSV change detection (polling)
        self.csv_check_interval = int(os.environ.get('SAMSUNG_TV_ART_CSV_CHECK_SECONDS', '60'))
        self._csv_last_check = 0
        self._csv_mtime = None
        try:
            self.wait_for_csv_seconds = int(os.environ.get('SAMSUNG_TV_ART_WAIT_FOR_CSV_SECONDS', '120'))
        except Exception:
            self.wait_for_csv_seconds = 120
        self.require_csv_on_start = os.environ.get('SAMSUNG_TV_ART_REQUIRE_CSV_ON_START', 'true').lower() in ['1', 'true', 'yes']
        # Startup selections are driven by retained MQTT only; no default env selection
        # Memory logging interval in seconds (0 disables)
        try:
            self.memlog_seconds = int(os.environ.get('SAMSUNG_TV_ART_MEMLOG_SECONDS', '0'))
        except Exception:
            self.memlog_seconds = 0
        # Periodic MQTT state refresh (seconds). Publishes current TV artwork even if unchanged
        try:
            self.state_refresh_seconds = int(os.environ.get('SAMSUNG_TV_ART_STATE_REFRESH_SECONDS', '300'))
        except Exception:
            self.state_refresh_seconds = 300
        self._last_state_publish = 0
        self._refresh_in_progress = False
        self._loop = None
        self._collections_sync_running = False
        # Optional mirror directory for Home Assistant media (e.g., bind-mounted /media)
        self.mirror_dir = os.environ.get('SAMSUNG_TV_ART_MIRROR_DIR')
        try:
            if self.mirror_dir and not os.path.isdir(self.mirror_dir):
                os.makedirs(self.mirror_dir, exist_ok=True)
        except Exception:
            # If we fail to create, disable mirroring silently
            self.mirror_dir = None
        try:
            #doesn't work in Windows
            asyncio.get_running_loop().add_signal_handler(SIGINT, self.close)
            asyncio.get_running_loop().add_signal_handler(SIGTERM, self.close)
        except Exception:
            pass
    
    def _map_to_artwork_dir(self, name: str):
        """Translate a provided collection name (possibly artist_name with spaces)
        to the on-disk folder (artwork_dir). Returns a valid folder name or None."""
        try:
            if not name:
                return None
            # If already a valid folder, keep as-is
            path = os.path.join(self.media_root, name)
            if os.path.isdir(path):
                return name
            # Try direct directory normalization (works even before CSV metadata is loaded)
            resolved = self._resolve_dir_from_name(name)
            if resolved:
                return resolved
            # Try CSV artist_name -> artwork_dir mapping (populated by _load_csv_metadata)
            amap = getattr(self, '_artist_to_dir', {})
            if amap:
                dn = amap.get(name) or amap.get(self._normalize_collection_key(name))
                if dn and os.path.isdir(os.path.join(self.media_root, dn)):
                    return dn
        except Exception:
            pass
        return None

    def _normalize_collection_key(self, value: str) -> str:
        try:
            s = str(value or '').strip().lower()
            s = unicodedata.normalize('NFKD', s)
            s = ''.join(ch for ch in s if not unicodedata.combining(ch))
            s = s.replace('_', ' ')
            s = ' '.join(s.split())
            return s
        except Exception:
            return str(value or '').strip().lower()

    def _resolve_dir_from_name(self, name: str):
        try:
            target = self._normalize_collection_key(name)
            if not target:
                return None
            for d in self._scan_collections():
                if self._normalize_collection_key(d) == target:
                    return d
            # common fallback: spaces in labels vs underscores on disk
            underscored = target.replace(' ', '_')
            for d in self._scan_collections():
                if d.lower() == underscored.lower():
                    return d
        except Exception:
            pass
        return None
    
    def _create_tv_connection(self):
        """Create TV connection object. May raise if TV is unreachable."""
        from samsungtvws.async_art import SamsungTVAsyncArt
        self.tv = SamsungTVAsyncArt(host=self.ip, port=8002, token_file=self.token_file)
        
    async def start_monitoring(self):
        '''
        program entry point
        '''
        try:
            self._loop = asyncio.get_running_loop()
        except Exception:
            self._loop = None
        # Create TV connection (may raise if TV offline)
        self._create_tv_connection()
        # Ensure CSV metadata is loaded before MQTT and selection logic.
        if self.require_csv_on_start:
            ready = await self._wait_for_csv_metadata()
            if not ready:
                raise RuntimeError(f'CSV metadata unavailable at startup: {self.csv_path}')
        else:
            self._load_csv_metadata()
        # Init MQTT if enabled
        if self.mqtt_enabled:
            self._init_mqtt()
        # Start periodic memory logging if enabled
        try:
            if getattr(self, 'memlog_seconds', 0) > 0:
                asyncio.create_task(self._memlogger())
        except Exception:
            pass
        
        if self.on and not await self.tv.on():
            self.log.info('TV is off, exiting')
        else:
            self.log.info('Start Monitoring')
            try:
                await self.tv.start_listening()
                self.log.info('Started')
            except Exception as e:
                self.log.error('failed to connect with TV: {}'.format(e))
            if self.tv.is_alive():
                try:
                    await self.check_matte()
                    await self.ensure_standby_selected()
                    await self.cleanup_old_uploads()
                    # Publish MQTT discovery early so entity exists at startup
                    if self.mqtt_enabled:
                        self._publish_mqtt_discovery()
                        # Also publish collections + settings on startup
                        self._publish_collections_discovery()
                        self._publish_settings_discovery()
                        await asyncio.sleep(0)  # yield before heavy scan
                        self._publish_collections_state()
                        self._publish_settings_state()
                        # Do not restore from cache on startup; retained MQTT selection will drive state
                        pass
                    await self.select_artwork()
                finally:
                    await self.tv.close()
            else:
                await self.tv.close()

    async def _wait_for_csv_metadata(self):
        """Wait for CSV file and metadata headers to be available before startup continues."""
        timeout = max(0, int(getattr(self, 'wait_for_csv_seconds', 0)))
        deadline = time.time() + timeout if timeout > 0 else None
        announced_wait = False
        while True:
            try:
                if self.csv_path and os.path.isfile(self.csv_path):
                    self._load_csv_metadata()
                    if self._csv_headers:
                        return True
                if deadline is not None and time.time() >= deadline:
                    self.log.error('Timed out waiting for CSV metadata at %s after %ss', self.csv_path, timeout)
                    return False
                if not announced_wait:
                    if timeout > 0:
                        self.log.info('Waiting for CSV metadata at %s (timeout %ss) before continuing startup', self.csv_path, timeout)
                    else:
                        self.log.info('Waiting for CSV metadata at %s before continuing startup', self.csv_path)
                    announced_wait = True
                await asyncio.sleep(1)
            except Exception as e:
                self.log.warning('Error while waiting for CSV metadata: %s', e)
                if deadline is not None and time.time() >= deadline:
                    return False
                await asyncio.sleep(1)

    async def reconnect_tv(self):
        """Gracefully reconnect to the TV websocket."""
        try:
            await self.tv.close()
        except Exception:
            pass
        for attempt in range(1, 6):
            try:
                await asyncio.sleep(self.reconnect_delay * attempt)
                await self.tv.start_listening()
                if self.tv.is_alive():
                    self.log.info('Reconnected to TV on attempt %d', attempt)
                    return True
            except Exception as e:
                self.log.warning('Reconnect attempt %d failed: %s', attempt, e)
        return False

    async def safe_in_artmode(self):
        """Return True if TV reports art mode; False on any error. Uses exponential backoff."""
        try:
            self.last_artmode_check = time.time()
            in_artmode = await self.tv.in_artmode()
            # Success - reset failure counter
            self.consecutive_failures = 0
            return in_artmode
        except AssertionError:
            self.consecutive_failures += 1
            self.log.warning('TV artmode check failed (empty response, failure %d); treating as off', self.consecutive_failures)
            return False
        except Exception as e:
            self.consecutive_failures += 1
            self.log.warning('TV artmode check failed (failure %d): %s', self.consecutive_failures, e)
            return False

    def get_backoff_delay(self):
        """Calculate exponential backoff delay based on consecutive failures."""
        if self.consecutive_failures <= 1:
            return self.artmode_refresh_seconds or 60
        # Exponential backoff: 60, 120, 240, 480, 960, up to max_backoff_seconds
        delay = min(60 * (2 ** (self.consecutive_failures - 1)), self.max_backoff_seconds)
        return delay

    async def cleanup_old_uploads(self):
        """Delete previously uploaded photos from the TV in small batches to avoid overwhelming it."""
        try:
            if not await self.safe_in_artmode():
                self.log.info('TV not in art mode, skipping cleanup')
                return

            my_photos = await self.get_tv_content('MY-C0002')
            if my_photos:
                if self.standby_content_id:
                    my_photos = [cid for cid in my_photos if cid != self.standby_content_id]
                if my_photos:
                    self.log.info('Cleaning up %d existing uploads from TV...', len(my_photos))
                    # Let TV settle before starting deletions
                    await asyncio.sleep(5)
                    # Delete ONE AT A TIME with delays to be very gentle on TV WiFi
                    for i, content_id in enumerate(my_photos):
                        await self.tv.delete_list([content_id])
                        self.log.debug('Deleted %d/%d', i + 1, len(my_photos))
                        # Wait between each delete
                        await asyncio.sleep(self.delete_delay_seconds)
                    # Give TV significant time to recover after all deletions
                    self.log.info('Waiting for TV to recover after deletions...')
                    await asyncio.sleep(30)
            self.cache = {}
            self.current_key = None
            try:
                if os.path.isfile(self.cache_path):
                    os.remove(self.cache_path)
            except Exception as e:
                self.log.warning('Failed to remove cache file: %s', e)
        except Exception as e:
            self.log.warning('Failed to cleanup uploads: %s', e)

    async def ensure_standby_selected(self):
        """Upload and select standby image if present.
        Idempotent: skips the upload when the standby is already active in this session.
        """
        if not self.standby:
            return
        standby_path = self.standby if os.path.isabs(self.standby) else os.path.join(self.media_root, self.standby)
        if not os.path.isfile(standby_path):
            self.log.warning('Standby file not found on disk: %s', standby_path)
            return
        if self.standby_content_id:
            self.log.debug('Standby already active as %s; skipping re-upload', self.standby_content_id)
            return
        try:
            file_data, file_type = self.read_file(standby_path)
            if file_data and self.tv.art_mode:
                content_id = await self.tv.upload(file_data, file_type=file_type, matte=self.matte, portrait_matte=self.matte)
                if content_id:
                    self.standby_content_id = content_id
                    await self.tv.select_image(content_id)
                    # Mirror standby for card fallback if mirroring is enabled
                    try:
                        self._mirror_add('standby.png', standby_path)
                    except Exception:
                        pass
                    # Publish standby via MQTT if enabled
                    if self.mqtt_enabled:
                        self._publish_mqtt_discovery()
                        self._publish_mqtt_state('Standby', 'standby.png', os.path.basename(os.path.dirname(standby_path)) or None)
                    self.log.info('Selected standby: %s (%s)', self.standby, content_id)
        except Exception as e:
            self.log.warning('Failed to upload/select standby: %s', e)

    def get_selected_folder(self):
        """Return selected folder based on MQTT-driven selection.

        Uses the first selected collection (if any); otherwise keeps current folder.
        """
        if self.selected_collections:
            return os.path.join(self.media_root, self.selected_collections[0])
        return self.folder

    def apply_selection(self):
        """Update folder if the selection changed (via MQTT)."""
        previous_collections = self.selected_collections.copy()
        desired = self.get_selected_folder()
        collections_changed = (self.selected_collections != previous_collections) or self._pending_selection_change
        if desired != self.folder or collections_changed:
            if not os.path.isdir(desired):
                self.log.warning('Selected folder does not exist: %s', desired)
                return False
            self.log.info('Selection changed, switching folder to %s', desired)
            if collections_changed:
                self.log.info('Collections changed: %s', self.selected_collections)
            self.folder = desired
            self.fav = set()
            self.shown_content_ids = set()  # Reset shuffle tracking on collection change
            self.set_current_cache()
            self._pending_selection_change = False
            return True
        return False

    def get_cache_key(self, folder_path):
        try:
            return os.path.relpath(folder_path, self.media_root)
        except Exception:
            return folder_path

    def load_cache(self):
        if self.cache and self.current_key is not None:
            return
        if os.path.isfile(self.cache_path):
            try:
                with open(self.cache_path, 'r') as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}
        else:
            self.cache = {}

    def _read_cached_selected_collections(self):
        try:
            self.load_cache()
            return self.cache.get('_selected_collections', [])
        except Exception:
            return []

    def save_cache(self):
        try:
            with open(self.cache_path, 'w') as f:
                json.dump(self.cache, f)
        except Exception as e:
            self.log.warning('Failed to save cache: %s', e)

    def set_current_cache(self):
        self.load_cache()
        self.current_key = self.get_cache_key(self.folder)
        data = self.cache.get(self.current_key, {})
        self.uploaded_files = data.get('uploaded_files', {})
        self.start = data.get('last_update', time.time())

    def _cache_selected_collections(self):
        try:
            self.load_cache()
            self.cache['_selected_collections'] = list(self.selected_collections)
            self.save_cache()
        except Exception as e:
            self.log.warning('Failed to cache selected_collections: %s', e)
        
    def close(self):
        '''
        exit on signal
        '''
        self.log.info('SIGINT/SIGTERM received, exiting')
        os._exit(1)
        
    async def get_api_version(self):
        '''
        checks api version to see if it's old (<2021) or new type
        sets api_version to 0 for old, and 1 for new
        '''
        try:
            api_version = await self.tv.get_api_version()
            self.log.info('API version: {}'.format(api_version))
            self.api_version = 0 if int(api_version.replace('.','')) < 4000 else 1
        except Exception as e:
            self.log.warning('Failed to get API version: %s', e)
            self.api_version = 0
        
    async def check_matte(self):
        '''
        checks if the matte passed for uploads to use is valid type and color
        '''
        if self.matte != 'none':
            matte = self.matte.split('_')
            try:
                mattes = await self.tv.get_matte_list(True)
                matte_types, matte_colors = ([m['matte_type'] for m in mattes[0]], [m['color'] for m in mattes[1]])
                if matte[0] in matte_types and matte[1] in matte_colors:
                    self.log.info('using matte: {}'.format(self.matte))
                    return
                else:
                    self.log.info('Valid mattes types: {} and colors: {}'.format(matte_types, matte_colors))
                self.log.warning('Invalid matte selected: {}. A valid matte would be shadowbox_polar for eample, using none'.format(self.matte))
            except AssertionError:
                self.log.warning('Error getting mattes list, setting to none')
            self.matte = 'none'
            
    async def initialize(self):
        '''
        initializes program
        gets API version, and current displayed art content_id
        uses PIL if available to try to match files in folder with content_id on tv.
        this matching is not really needed if uploaded_files (loaded from file) is accurate,
        and can be skipped by setting sync (-s) to False
        '''
        await self.get_api_version()
        self.current_content_id = await self.get_current_artwork()
        self.log.info('Current artwork is: {}'.format(self.current_content_id))
        # Publish current state at startup to avoid stale retained values
        try:
            await self._publish_current_artwork_state(force=True)
        except Exception:
            pass
        # Fallback selection: if nothing selected via MQTT, restore cached selection
        # or auto-select all available collections so we don't sit on standby only.
        try:
            if not self.selected_collections:
                cached = self._read_cached_selected_collections()
                mapped = []
                try:
                    have = set(self._scan_collections())
                except Exception:
                    have = set()
                if cached:
                    for c in cached:
                        mc = self._map_to_artwork_dir(c) or c
                        if mc in have and mc not in mapped:
                            mapped.append(mc)
                # If no cached (or none valid), default to all available collections
                if not mapped:
                    mapped = sorted(list(have))
                if mapped:
                    self.selected_collections = mapped
                    self._pending_selection_change = True
                    # Reflect fallback selection on the shared state so UIs stay consistent
                    try:
                        self._publish_selected_collections_state()
                    except Exception:
                        pass
                    self.log.info('No MQTT selection found; using fallback collections: %s', self.selected_collections)
        except Exception:
            # Non-fatal; continue with no selection
            pass
        self.load_program_data()
        self.log.info('files in directory: {}: {}'.format(self.folder, self.get_folder_files()))
        if self.sync:
            await self.pil.initialize() #optional
        else:
            self.log.warning('syncing disabled, not updating uploaded files list')
        
        # Force immediate art display after initialization if we have content
        # This ensures we don't sit on standby for 30 mins after restart
        if len(self.get_content_ids()) > 0:
            self.log.info('Content available after init, displaying first artwork immediately')
            await self.change_art()
            self.start = time.time()
            self.write_program_data()
        
    async def get_tv_content(self, category='MY-C0002'):
        '''
        gets content_id list of category - either My Photos (MY-C0002) or Favourites (MY-C0004) from tv
        '''
        try:
            result = [v['content_id'] for v in await self.tv.available(category, timeout=10)]
        except AssertionError:
            self.log.warning('failed to get contents from TV')
            result = None
        except Exception as e:
            self.log.warning('failed to get contents from TV: %s', e)
            result = None
        return result
        
    def get_folder_files(self):
        '''
        returns list of files in folder is extension matches allowed image types
        '''
        return [f for f in os.listdir(self.folder) if os.path.isfile(os.path.join(self.folder, f)) and self.get_file_type(os.path.join(self.folder, f)) in self.allowed_ext]
        
    async def get_current_artwork(self):
        '''
        reads currently displayed art content_id from tv
        '''
        try:
            content_id = (await self.tv.get_current()).get('content_id')
        except Exception:
            content_id = None
        return content_id
        
            
    async def sync_file_list(self):
        '''
        if art has been deleted on tv, resyncronises uploaded_files with tv
        '''
        my_photos = await self.get_tv_content('MY-C0002')
        if my_photos is not None:
            self.uploaded_files = {k:v for k,v in self.uploaded_files.items() if v['content_id'] in my_photos}
            self.write_program_data()
        
    def get_time(self, sec):
        '''
        returns seconds as timedelta for display as h:m:s
        '''
        return datetime.timedelta(seconds = sec)
   
    def load_program_data(self):
        '''
        load previous settings on program start update
        '''
        self.set_current_cache()
        
    def write_program_data(self):
        '''
        save current settings, including file list with content_id on tv and last updated time
        also save the last time that art was updated, for timing slideshows
        '''
        program_data = {'last_update': self.start, 'uploaded_files': self.uploaded_files}
        try:
            with open(self.program_data_path, 'w') as f:
                json.dump(program_data, f)
        except Exception as e:
            self.log.warning('Failed to save program data: %s', e)

        self.load_cache()
        key = self.get_cache_key(self.folder)
        self.cache[key] = program_data
        # Persist current selected_collections for restart restore
        self.cache['_selected_collections'] = list(self.selected_collections)
        self.save_cache()
            
    def read_file(self, filename):
        '''
        read image file, return file binary data and file type
        Resizes images larger than 4K to 4K to ensure compatibility with Samsung Frame TV
        '''
        try:
            with open(filename, 'rb') as f:
                file_data = f.read()
            file_type = self.get_file_type(filename)
            
            # Resize if necessary
            if HAVE_PIL and file_data:
                try:
                    img = Image.open(io.BytesIO(file_data))
                    if img.width > 3840 or img.height > 2160:
                        self.log.info('Resizing image {} from {}x{} to fit 4K'.format(filename, img.width, img.height))
                        img.thumbnail((3840, 2160), Image.Resampling.LANCZOS)
                        output = io.BytesIO()
                        img.save(output, format=img.format or 'JPEG')
                        file_data = output.getvalue()
                        # Update file_type if changed
                        file_type = self.get_file_type(filename, img)
                except Exception as e:
                    self.log.warning('Failed to resize image {}: {}'.format(filename, e))
            
            return file_data, file_type
        except Exception as e:
            self.log.error('Error reading file: {}, {}'.format(filename, e))
        return None, None
        
    def get_file_type(self, filename, image_data=None):
        '''
        try to figure out what kind of image file is, starting with the extension
        use PIL if available to check
        fix the file type if it's wrong
        '''
        try:
            file_type = os.path.splitext(filename)[1][1:].lower()
            file_type = file_type.lower() if file_type else None
            # Fast-path for clearly non-image extensions to avoid noisy PIL errors
            if image_data is None and file_type and file_type not in self.allowed_ext:
                return file_type
            file_type = self.pil.fix_file_type(filename, file_type, image_data)
            return file_type
        except Exception as e:
            self.log.error('Error reading file: {}, {}'.format(filename, e))
        return None

    def _get_rss_kb(self):
        try:
            with open('/proc/self/status', 'r') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            return int(parts[1])  # kB
        except Exception:
            pass
        return None

    async def _memlogger(self):
        while True:
            try:
                rss_kb = self._get_rss_kb()
                fd_count = 0
                try:
                    fd_count = len(os.listdir('/proc/self/fd'))
                except Exception:
                    fd_count = -1
                if rss_kb is not None:
                    self.log.info('Memory usage: RSS=%.2f MB, FDs=%s', rss_kb / 1024.0, fd_count)
                else:
                    self.log.info('Memory usage: RSS=unknown, FDs=%s', fd_count)
            except Exception:
                pass
            await asyncio.sleep(max(5, int(getattr(self, 'memlog_seconds', 60))))
            
    def update_uploaded_files(self, filename, content_id, full_path=None):
        '''
        if file is uploaded, update the dictionary entry
        if content_id is None, file failed to upload, so remove it from the dict
        full_path is used for multi-collection mode where filename is just basename
        '''
        self.uploaded_files.pop(filename, None)
        if content_id:
            rel_path = None
            try:
                if full_path and self.media_root and os.path.commonpath([self.media_root, full_path]) == self.media_root:
                    rel_path = os.path.relpath(full_path, self.media_root)
            except Exception:
                rel_path = None
            self.uploaded_files[filename] = {
                'content_id': content_id,
                'modified': self.get_last_updated(filename, full_path),
                'path_rel': rel_path or filename
            }
        
    async def upload_files(self, filenames):
        '''
        upload files in list to tv with rate limiting to avoid overwhelming TV
        Supports both simple filenames (from current folder) and relative paths (from multi-collection mode).
        '''
        upload_delay = self.upload_delay_seconds  # seconds between uploads
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        for idx, filename in enumerate(filenames):
            # Handle both simple filenames and relative paths from multi-collection mode
            if os.path.dirname(filename):
                # Multi-collection mode: filename includes collection subfolder
                path = os.path.join(self.media_root, filename)
                display_name = filename  # Show full relative path in logs
            else:
                # Single folder mode: simple filename
                path = os.path.join(self.folder, filename)
                display_name = filename
            
            # Verify file exists before attempting upload
            if not os.path.isfile(path):
                self.log.error('File not found: %s', path)
                continue
                
            file_data, file_type = self.read_file(path)
            if file_data and self.tv.art_mode:
                self.log.info('uploading : {} to tv ({}/{})'.format(display_name, idx + 1, len(filenames)))
                content_id = None
                try:
                    content_id = await self.tv.upload(file_data, file_type=file_type, matte=self.matte, portrait_matte=self.matte)
                    consecutive_failures = 0  # Reset on success
                except AssertionError:
                    self.log.warning('file: %s failed to upload (empty response)', display_name)
                    consecutive_failures += 1
                except Exception as e:
                    self.log.warning('file: %s failed to upload: %s', display_name, e)
                    consecutive_failures += 1
                
                # If too many consecutive failures, try reconnecting to TV
                if consecutive_failures >= max_consecutive_failures:
                    self.log.warning('Multiple consecutive upload failures, attempting TV reconnect...')
                    await self.reconnect_tv()
                    await asyncio.sleep(5)
                    consecutive_failures = 0
                    # Skip to next file after reconnect
                    continue
                    
                # Use basename for uploaded_files tracking (consistent with single-folder mode)
                # Pass full path for get_last_updated in multi-collection mode
                base_name = os.path.basename(filename)
                self.update_uploaded_files(base_name, content_id, full_path=path)
                if self.uploaded_files.get(base_name, {}).get('content_id'):
                    self.log.info('uploaded : {} to tv as {}'.format(display_name, self.uploaded_files[base_name]['content_id']))
                    # Mirror to media directory for Home Assistant, if configured (preserve relative path when available)
                    rel_for_copy = filename if os.path.dirname(filename) else base_name
                    self._mirror_add(rel_for_copy, path)
                else:
                    self.log.warning('file: {} failed to upload'.format(display_name))
                self.write_program_data()
                # Add delay between uploads to let TV process
                if idx < len(filenames) - 1:
                    await asyncio.sleep(upload_delay)
            
    async def delete_files_from_tv(self, content_ids):
        '''
        remove files from tv if tv is in art mode
        '''
        if self.tv.art_mode:
            self.log.info('removing files from tv : {}'.format(content_ids))
            await self.tv.delete_list(content_ids)
            await self.sync_file_list()

    def get_last_updated(self, filename, full_path=None):
        '''
        get last updated timestamp for file
        If full_path is provided, use it directly. Otherwise construct from self.folder + filename.
        '''
        if full_path:
            return os.path.getmtime(full_path)
        return os.path.getmtime(os.path.join(self.folder, filename))
        
    async def remove_files(self, files):
        '''
        if files deleted, remove them from tv
        '''
        # Determine which basenames are removed
        removed_basenames = [k for k in list(self.uploaded_files.keys()) if k not in files]
        content_ids_removed = [self.uploaded_files[k]['content_id'] for k in removed_basenames]
        #delete images from tv
        if content_ids_removed:
            await self.delete_files_from_tv(content_ids_removed)
            # Mirror removals locally (preserve relative path if known)
            for base in removed_basenames:
                rel = self.uploaded_files.get(base, {}).get('path_rel', base)
                self._mirror_remove(rel)
            return True
        return False
            
    async def add_files(self, files):
        '''
        if new files found, upload to tv
        Limits uploads to avoid overwhelming the TV - we only need a few images for rotation
        When one or more collections are selected, uses collection-based randomization.
        '''
        max_uploads = int(os.environ.get('SAMSUNG_TV_ART_MAX_UPLOADS', '10'))
        
        # If collections are selected, always source candidates from those collections
        # (works for single and multi-collection modes).
        collections = getattr(self, 'selected_collections', [])

        if len(collections) > 0:
            new_files = await self.get_files_from_multiple_collections(collections, max_uploads)
        else:
            # Fallback: legacy single-folder mode when no collections are selected
            new_files = [f for f in files if f not in self.uploaded_files.keys()]
            if len(new_files) > max_uploads:
                self.log.info('Limiting upload from %d to %d files to protect TV', len(new_files), max_uploads)
                # Always pick a random sample for variety when changing collections
                new_files = random.sample(new_files, max_uploads)
        
        #upload new files
        if new_files:
            # Sort for sequential playback if enabled
            if self.sequential:
                new_files = sorted(new_files)
            self.log.info('adding files to tv : {}'.format(new_files))
            await self.wait_for_files(new_files)
            await self.upload_files(new_files)
            return len(new_files)
        return 0

    async def get_files_from_multiple_collections(self, collections, max_uploads):
        '''
        Get files evenly distributed from multiple collections.
        If max_uploads=8 and collections=2, gets 4 from each.
        Returns list of full paths relative to media_root.
        '''
        num_collections = len(collections)
        per_collection = max_uploads // num_collections
        remainder = max_uploads % num_collections
        
        self.log.info('Distributing %d uploads across %d collections (%d each, %d extra)', 
                      max_uploads, num_collections, per_collection, remainder)
        
        all_files = []
        for idx, collection in enumerate(collections):
            collection_path = os.path.join(self.media_root, collection)
            if not os.path.isdir(collection_path):
                self.log.warning('Collection directory not found: %s', collection_path)
                continue
            
            # Get files from this collection
            try:
                collection_files = [
                    f for f in os.listdir(collection_path) 
                    if os.path.isfile(os.path.join(collection_path, f)) 
                    and self.get_file_type(os.path.join(collection_path, f)) in self.allowed_ext
                ]
            except Exception as e:
                self.log.warning('Failed to list collection %s: %s', collection, e)
                continue
            
            # How many to take from this collection
            take_count = per_collection + (1 if idx < remainder else 0)
            
            if len(collection_files) > take_count:
                selected = random.sample(collection_files, take_count)
            else:
                selected = collection_files
            
            # Store with collection prefix for tracking
            for f in selected:
                # Use relative path from media_root for proper folder handling
                all_files.append(os.path.join(collection, f))
            
            self.log.info('Selected %d files from %s', len(selected), collection)
        
        # Filter out already uploaded files
        new_files = [f for f in all_files if os.path.basename(f) not in self.uploaded_files.keys()]
        self.log.info('Total new files to upload: %d', len(new_files))
        return new_files
            
    async def update_files(self, files):
        '''
        check if files were modified
        if so, delete old content on tv and upload new
        '''
        modified_files = [f for f in files if f in self.uploaded_files.keys() and self.uploaded_files[f].get('modified') != self.get_last_updated(f)]
        #delete old file and upload new:
        if modified_files:
            self.log.info('updating files on tv : {}'.format(modified_files))
            await self.wait_for_files(modified_files)
            files_to_delete = [v['content_id'] for k, v in self.uploaded_files.items() if k in modified_files]
            await self.delete_files_from_tv(files_to_delete)
            await self.upload_files(modified_files)
            return True
        return False

    def _mirror_add(self, rel_path, src_full_path):
        """Copy file to mirror directory as <mirror>/<rel_path> if enabled."""
        if not self.mirror_dir:
            return
        try:
            import shutil
            # Normalize rel_path to avoid escaping mirror_dir
            rel_norm = os.path.normpath(rel_path).lstrip(os.sep)
            dest = os.path.join(self.mirror_dir, rel_norm)
            # Ensure parent exists
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src_full_path, dest)
            self.log.debug('Mirrored %s -> %s', src_full_path, dest)
        except Exception as e:
            self.log.debug('Mirror add failed for %s: %s', rel_path, e)

    def _mirror_remove(self, rel_path):
        """Remove mirrored file <mirror>/<rel_path> if present."""
        if not self.mirror_dir:
            return
        try:
            rel_norm = os.path.normpath(rel_path).lstrip(os.sep)
            dest = os.path.join(self.mirror_dir, rel_norm)
            if os.path.isfile(dest):
                os.remove(dest)
                self.log.debug('Removed mirrored file %s', dest)
        except Exception as e:
            self.log.debug('Mirror remove failed for %s: %s', rel_path, e)

    def _mirror_clear_all(self):
        """Clear all mirrored files and empty directories under mirror_dir.
        Standby will be mirrored again by ensure_standby_selected() if configured.
        """
        if not self.mirror_dir:
            return
        try:
            if not os.path.isdir(self.mirror_dir):
                return
            for root, dirs, files in os.walk(self.mirror_dir, topdown=False):
                for name in files:
                    path = os.path.join(root, name)
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                for d in dirs:
                    dpath = os.path.join(root, d)
                    try:
                        os.rmdir(dpath)
                    except Exception:
                        # Not empty or can't remove — ignore
                        pass
            self.log.info('Cleared mirror directory: %s', self.mirror_dir)
        except Exception as e:
            self.log.debug('Mirror clear failed: %s', e)

    def _mirror_prune_to_selected(self):
        """Remove mirrored files that are not part of the currently selected collections.
        Keeps:
          - standby.png
          - Files under <collection>/<file> where <collection> is selected and file exists in media_root
          - Top-level files (no subfolder) whose basename exists in any selected collection (legacy single-folder mirror)
        If no collections are selected, removes all except standby.png.
        """
        if not self.mirror_dir or not os.path.isdir(self.mirror_dir):
            return
        try:
            selected = list(self.selected_collections or [])
            # Build allowed sets from media_root for quick membership checks
            present_relpaths = set()
            present_basenames = set()
            for col in selected:
                col_dir = os.path.join(self.media_root, col)
                if not os.path.isdir(col_dir):
                    continue
                try:
                    for fname in os.listdir(col_dir):
                        fullp = os.path.join(col_dir, fname)
                        if os.path.isfile(fullp):
                            present_relpaths.add(os.path.join(col, fname))
                            present_basenames.add(fname)
                except Exception:
                    continue

            deletions = 0
            for root, _dirs, files in os.walk(self.mirror_dir):
                for name in files:
                    # Always keep standby
                    if name.lower() == 'standby.png':
                        continue
                    fpath = os.path.join(root, name)
                    rel = os.path.normpath(os.path.relpath(fpath, self.mirror_dir)).lstrip(os.sep)
                    keep = False
                    if selected:
                        if os.sep in rel:
                            # Expecting collection/filename
                            parts = rel.split(os.sep, 1)
                            collection, rest = parts[0], parts[1]
                            if collection in selected and os.path.join(collection, os.path.basename(rest)) in present_relpaths:
                                keep = True
                        else:
                            # Top-level file; keep if basename exists in any selected collection
                            if rel in present_basenames:
                                keep = True
                    else:
                        # No selection: remove everything except standby
                        keep = False

                    if not keep:
                        try:
                            os.remove(fpath)
                            deletions += 1
                        except Exception:
                            pass
            # Clean up empty dirs
            for root, dirs, _files in os.walk(self.mirror_dir, topdown=False):
                for d in dirs:
                    dpath = os.path.join(root, d)
                    try:
                        os.rmdir(dpath)
                    except Exception:
                        pass
            if deletions:
                self.log.info('Pruned %d mirrored files not in selected collections', deletions)
        except Exception as e:
            self.log.debug('Mirror prune failed: %s', e)
            
    async def wait_for_files(self, files):
        #wait for files to arrive
        await asyncio.sleep(min(10, 5 * len(files)))
            
    async def update_art_timer(self):
        '''
        changes art on tv as part of slideshow if enabled
        updates favourites list if favourites are included in slideshow
        '''
        if self.update_time > 0 and (len(self.uploaded_files.keys()) > 1 or self.include_fav):
            if time.time() - self.start >= self.update_time:
                self.log.info('doing slideshow update, after {}'.format(self.get_time(self.update_time)))
                self.start = time.time()
                self.write_program_data()
                if self.include_fav:
                    self.log.info('updating favourites')
                    fav = await self.get_tv_content('MY-C0004')
                    self.fav = set(fav) if fav is not None else self.fav
                await self.change_art()
            else:
                self.log.info('next {} update in {}'.format('sequential' if self.sequential else 'random', self.get_time(self.update_time - (time.time() - self.start))))
                
    def get_content_ids(self):
        '''
        return list of all content ids available for selecting to display NOTE sets() are not ordered
        if not including favourites, order list by filename in self.uploaded_files
        '''
        if self.fav:
            # Exclude from uploaded files and fav
            uploaded_ids = {v['content_id'] for k, v in self.uploaded_files.items() if k not in self.exclude and v['content_id'] not in self.exclude_content_ids}
            fav_ids = self.fav - set(self.exclude_content_ids)
            return list(uploaded_ids.union(fav_ids))
        return [v['content_id'] for k, v in sorted(self.uploaded_files.items()) if k not in self.exclude and v['content_id'] not in self.exclude_content_ids]
        
    def get_next_art(self):
        '''
        get next content_id from list, using shuffle-without-repeat logic.
        Shows all images once before any repeats (like shuffling a deck of cards).
        '''
        all_content_ids = self.get_content_ids()
        if not all_content_ids:
            return None
        
        # Get unshown images (excluding current)
        unshown = [cid for cid in all_content_ids if cid not in self.shown_content_ids and cid != self.current_content_id]
        
        # If all images have been shown, reset the cycle
        if not unshown:
            self.log.info('All %d images shown, starting new shuffle cycle', len(self.shown_content_ids))
            self.shown_content_ids = set()
            # Exclude only current image for the new cycle
            unshown = [cid for cid in all_content_ids if cid != self.current_content_id]
        
        if unshown:
            if self.sequential:
                # Sequential: pick next in sorted order from unshown
                content_id = sorted(unshown)[0]
            else:
                # Random: pick randomly from unshown
                content_id = random.choice(unshown)
            return content_id
        
        # Fallback: only one image exists
        return all_content_ids[0] if all_content_ids else None

    def get_filename_for_content_id(self, content_id):
        if not content_id:
            return None
        for filename, data in self.uploaded_files.items():
            if data.get('content_id') == content_id:
                return filename
        return None

    # HA REST methods removed in MQTT-only build

    async def update_ha_selected_artwork(self, content_id):
        filename = self.get_filename_for_content_id(content_id)
        if not filename:
            return
        # Resolve to full path and collection when possible
        base_name = os.path.basename(filename)
        rec = self.uploaded_files.get(base_name, {})
        rel_path = rec.get('path_rel')
        full_path = os.path.join(self.media_root, rel_path) if rel_path else os.path.join(self.folder, base_name)
        collection = None
        try:
            # Prefer collection from rel_path parent folder
            if rel_path:
                parts = os.path.normpath(rel_path).split(os.sep)
                if parts:
                    collection = parts[0]
            else:
                collection = os.path.basename(os.path.dirname(full_path))
        except Exception:
            collection = None
        display_name = os.path.splitext(base_name)[0]
        # Ensure mirrored file exists before publishing so UIs can load it immediately
        try:
            if self.mirror_dir and os.path.isfile(full_path):
                rel_for_copy = rel_path if rel_path else os.path.join(collection or '', base_name) if collection else base_name
                self._mirror_add(rel_for_copy, full_path)
        except Exception:
            pass
        # Consolidated payload
        state_obj = {"display": display_name, "file": base_name, "collection": collection}
        state_str = json.dumps(state_obj, separators=(",", ":"))
        try:
            if self.mqtt_enabled:
                self._publish_mqtt_discovery()
                self._publish_mqtt_state(display_name, base_name, collection)
        except Exception as e:
            self.log.warning('Failed to update Home Assistant selected artwork: %s', e)

    def _init_mqtt(self):
        if not self.mqtt_enabled or mqtt is None:
            return
        try:
            # paho-mqtt 2.x introduced CallbackAPIVersion. Prefer VERSION2 to avoid
            # deprecation warnings, but gracefully fall back to VERSION1 (paho 2.x)
            # or omit entirely (paho 1.x) when not available.
            _cb_cls = getattr(mqtt, 'CallbackAPIVersion', None)
            _cb_api = None
            if _cb_cls is not None:
                _cb_api = getattr(_cb_cls, 'VERSION2', None) or getattr(_cb_cls, 'VERSION1', None)
            _client_kwargs = dict(
                client_id=self._resolve_mqtt_client_id(),
                clean_session=True,
                protocol=getattr(mqtt, 'MQTTv311', 4),
            )
            if _cb_api is not None:
                _client_kwargs['callback_api_version'] = _cb_api
            self._mqtt = mqtt.Client(**_client_kwargs)
            if self.mqtt_username:
                self._mqtt.username_pw_set(self.mqtt_username, self.mqtt_password)
            # Setup callbacks and logging
            self._mqtt.on_connect = self._on_mqtt_connect
            self._mqtt.on_disconnect = self._on_mqtt_disconnect
            self._mqtt.on_message = self._on_mqtt_message
            try:
                # Use a compatibility wrapper so both paho v1 and v2 callback
                # signatures are supported without warnings or crashes.
                self._mqtt.on_publish = self._on_mqtt_publish_compat
            except Exception:
                pass
            try:
                self._mqtt.enable_logger()
            except Exception:
                pass
            try:
                self._mqtt.reconnect_delay_set(min_delay=5, max_delay=60)
            except Exception:
                pass
            # Connect and start network loop.
            # keepalive=30 keeps the connection alive through typical NAT session
            # timeouts (many routers close idle TCP sessions after 30-60 s).
            self._mqtt.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
            self._mqtt.loop_start()
            # Give it a brief moment to receive CONNACK
            for _ in range(20):
                if getattr(self, '_mqtt_is_connected', False):
                    break
                time.sleep(0.2)
            if not getattr(self, '_mqtt_is_connected', False):
                self.log.warning('MQTT: did not receive CONNACK yet; publishes may be dropped until connected')
            # Subscribe to topics once connected (also repeated in on_connect)
            if self.selection_from_mqtt:
                try:
                    self._mqtt.subscribe(self.selection_mqtt_topic, qos=1)
                except Exception:
                    pass
            try:
                self._mqtt.subscribe(f"{self.mqtt_cmd_prefix}/#", qos=1)
            except Exception:
                pass
            # Publish a diagnostic heartbeat to verify publish path
            try:
                self._mqtt.publish('frame_tv/diag/online', 'online', qos=0, retain=False)
            except Exception:
                pass
            self.log.info('MQTT connect initiated to %s:%d', self.mqtt_host, self.mqtt_port)
        except Exception as e:
            self.log.warning('MQTT init failed: %s', e)
            self._mqtt = None

    def _resolve_mqtt_client_id(self) -> str:
        """Return a stable, unique MQTT client_id for this container instance.
        Priority:
          1) Explicit override via SAMSUNG_TV_ART_MQTT_CLIENT_ID
          2) Persisted UUID in /data/client_id.txt (created on first run)
          3) HOSTNAME + short UUID suffix

        The final ID is sanitized to [A-Za-z0-9_-] and trimmed to <= 64 chars
        for broad broker compatibility.
        """
        try:
            # 1) Explicit override
            override = os.environ.get('SAMSUNG_TV_ART_MQTT_CLIENT_ID')
            if override:
                cid = override.strip()
            else:
                # 2) Persisted UUID in /data
                data_dir = '/data'
                cid_file = os.path.join(data_dir, 'client_id.txt')
                persisted = None
                try:
                    if os.path.isfile(cid_file):
                        with open(cid_file, 'r') as f:
                            persisted = f.read().strip()
                    else:
                        os.makedirs(data_dir, exist_ok=True)
                        persisted = str(uuid.uuid4())
                        # Write atomically
                        tmp_path = cid_file + '.tmp'
                        with open(tmp_path, 'w') as f:
                            f.write(persisted)
                        os.replace(tmp_path, cid_file)
                except Exception:
                    # Fall through to ephemeral if persistence fails
                    persisted = None

                host = os.environ.get('HOSTNAME') or socket.gethostname() or 'container'
                # 3) Compose ID
                suffix = (persisted or str(uuid.uuid4()))[:8]
                cid = f"frame-tv-art-{host}-{suffix}"

            # Sanitize and trim
            cid = re.sub(r'[^A-Za-z0-9_-]', '-', cid)
            if len(cid) > 64:
                cid = cid[:64]
            return cid
        except Exception:
            # Absolute fallback to a random UUID-based id
            return f"frame-tv-art-{str(uuid.uuid4())[:8]}"

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            topic = getattr(msg, 'topic', '')
            if self.selection_from_mqtt and topic == self.selection_mqtt_topic:
                payload = msg.payload.decode('utf-8') if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload or '')
                raw_cols = [c.strip() for c in (payload or '').split(',') if c.strip()]
                # Map retained selections to artwork_dir folder names when possible
                mapped = []
                try:
                    have = set(self._scan_collections())
                except Exception:
                    have = set()
                for c in raw_cols:
                    mc = self._map_to_artwork_dir(c) or c
                    # Keep only entries that exist as directories under media_root
                    if mc in have and mc not in mapped:
                        mapped.append(mc)
                if mapped != self.selected_collections:
                    self.selected_collections = mapped
                    self._pending_selection_change = True
                    # Immediately publish the mapped selection so UI/HA reflect it
                    try:
                        self._publish_selected_collections_state()
                    except Exception:
                        pass
                    self._cache_selected_collections()
                    self.log.info('Received MQTT selection update (mapped from %s): %s', raw_cols, self.selected_collections)
                return
            # Command handling
            if topic.startswith(f"{self.mqtt_cmd_prefix}/"):
                payload_raw = msg.payload.decode('utf-8') if isinstance(msg.payload, (bytes, bytearray)) else (msg.payload or '')
                self.log.info('Received MQTT command on %s: %s', topic, payload_raw if isinstance(payload_raw, str) else '<binary>')
                self._handle_mqtt_command(topic[len(self.mqtt_cmd_prefix)+1:], payload_raw)
        except Exception as e:
            self.log.warning('Failed handling MQTT message: %s', e)

    def _schedule_command_coro(self, coro, label='command'):
        """Schedule a coroutine from MQTT callback context onto the main event loop."""
        try:
            loop = self._loop
            if loop and loop.is_running():
                return asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            self.log.warning('Failed to schedule %s: %s', label, e)
        try:
            # If scheduling failed, ensure we close the coroutine to avoid warnings
            coro.close()
        except Exception:
            pass
        return None

    # MQTT callbacks (connection lifecycle)
    def _on_mqtt_connect(self, client, userdata, flags, rc, properties=None):  # properties for MQTTv5 compatibility
        try:
            rc_val = getattr(rc, 'value', rc)
            self._mqtt_is_connected = (rc_val == 0)
            if rc_val == 0:
                self.log.info('MQTT connected (CONNACK rc=0)')
                # Ensure subscriptions are in place after reconnects
                try:
                    if self.selection_from_mqtt:
                        client.subscribe(self.selection_mqtt_topic, qos=1)
                    client.subscribe(f"{self.mqtt_cmd_prefix}/#", qos=1)
                except Exception:
                    pass
                # Republish retained state so broker always has fresh data after reconnect
                try:
                    self._publish_collections_state()
                    self._publish_settings_state()
                except Exception:
                    pass
            else:
                self.log.warning('MQTT connect failed (rc=%s)', str(rc_val))
        except Exception:
            pass

    def _on_mqtt_disconnect(self, client, userdata, rc, properties=None):
        try:
            self._mqtt_is_connected = False
            rc_val = getattr(rc, 'value', rc)
            self.log.warning('MQTT disconnected (rc=%s)', str(rc_val))
        except Exception:
            pass

    def _on_mqtt_publish(self, client, userdata, mid):
        try:
            self.log.debug('MQTT published (mid=%s)', str(mid))
        except Exception:
            pass

    # paho v2 passes extra positional args (properties, reasonCode). Accept and ignore.
    def _on_mqtt_publish_compat(self, client, userdata, mid, *args, **kwargs):
        try:
            return self._on_mqtt_publish(client, userdata, mid)
        except Exception:
            pass

    def _publish_mqtt_discovery(self):
        if not self.mqtt_enabled or not self._mqtt or self._mqtt_config_published:
            return
        try:
            obj_id = self.mqtt_unique_id
            cfg_topic = f"{self.mqtt_discovery_prefix}/sensor/{obj_id}/config"
            device = {
                "identifiers": [f"frame_tv_art_{self.ip}"],
                "name": "Frame TV Art",
                "manufacturer": "Custom",
                "model": "Art Uploader",
            }
            payload = {
                "name": "Frame TV Selected Artwork",
                "default_entity_id": "sensor.frame_tv_art_selected_artwork",
                "state_topic": self.mqtt_state_topic,
                "json_attributes_topic": self.mqtt_attr_topic,
                "unique_id": obj_id,
                "icon": "mdi:image-text",
                "device": device,
                "availability_topic": f"{self.mqtt_state_topic}/availability",
                # Ensure entity is enabled by default in registry
                "enabled_by_default": True,
                "entity_registry_enabled_default": True,
            }
            try:
                self._publish_and_wait(cfg_topic, json.dumps(payload), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)
            # Mark available
            try:
                self._publish_and_wait(f"{self.mqtt_state_topic}/availability", "online", qos=1, retain=True)
            except Exception:
                self._mqtt.publish(f"{self.mqtt_state_topic}/availability", "online", qos=0, retain=True)

            # Also publish discovery for 'Selected Collections' state sensor
            sel_obj_id = 'frame_tv_art_selected_collections'
            sel_cfg_topic = f"{self.mqtt_discovery_prefix}/sensor/{sel_obj_id}/config"
            sel_payload = {
                "name": "Frame TV Selected Collections",
                "default_entity_id": "sensor.frame_tv_art_selected_collections",
                "state_topic": self.mqtt_selected_collections_state_topic,
                "json_attributes_topic": self.mqtt_selected_collections_attr_topic,
                "unique_id": sel_obj_id,
                "icon": "mdi:folder-multiple",
                "device": device,
                # Ensure entity is enabled by default in registry
                "enabled_by_default": True,
                "entity_registry_enabled_default": True,
            }
            try:
                self._publish_and_wait(sel_cfg_topic, json.dumps(sel_payload), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(sel_cfg_topic, json.dumps(sel_payload), qos=1, retain=True)

            self._mqtt_config_published = True
            self.log.info('Published MQTT discovery to %s and %s', cfg_topic, sel_cfg_topic)
        except Exception as e:
            self.log.warning('MQTT discovery publish failed: %s', e)

    def _publish_mqtt_state(self, display, file, collection):
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            # State = display text; attributes carry file and collection
            try:
                self._publish_and_wait(self.mqtt_state_topic, display or "", qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_state_topic, display or "", qos=0, retain=True)
            attrs = {"file": file or "", "collection": collection or ""}
            # Merge CSV columns (ensure every header key exists, even if blank)
            if self._csv_headers:
                row = self._csv_by_file.get(file or "") or {}
                for h in self._csv_headers:
                    # Keep original header key names to match CSV
                    attrs[h] = str(row.get(h, "") or "")
            try:
                self._publish_and_wait(self.mqtt_attr_topic, json.dumps(attrs, separators=(",", ":")), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_attr_topic, json.dumps(attrs, separators=(",", ":")), qos=0, retain=True)
        except Exception as e:
            self.log.warning('MQTT state publish failed: %s', e)

    def _scan_collections(self):
        try:
            entries = [d for d in os.listdir(self.media_root) if os.path.isdir(os.path.join(self.media_root, d))]
            # Filter common NAS/system folders
            return sorted([d for d in entries if d not in ['@eaDir', '@tmp']])
        except Exception as e:
            self.log.warning('Failed to scan collections in %s: %s', self.media_root, e)
            return []

    def _publish_collections_discovery(self):
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            obj_id = self.mqtt_collections_unique_id
            cfg_topic = f"{self.mqtt_discovery_prefix}/sensor/{obj_id}/config"
            device = {
                "identifiers": [f"frame_tv_art_{self.ip}"],
                "name": "Frame TV Art",
                "manufacturer": "Custom",
                "model": "Art Uploader",
            }
            payload = {
                "name": "Frame TV Art Collections",
                "default_entity_id": "sensor.frame_tv_art_collections",
                "state_topic": self.mqtt_collections_state_topic,
                "json_attributes_topic": self.mqtt_collections_attr_topic,
                "unique_id": obj_id,
                "icon": "mdi:folder-multiple-image",
                "device": device,
                # Ensure entity is enabled by default in registry
                "enabled_by_default": True,
                "entity_registry_enabled_default": True,
            }
            try:
                self._publish_and_wait(cfg_topic, json.dumps(payload), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)
        except Exception as e:
            self.log.warning('MQTT collections discovery publish failed: %s', e)

    def _publish_settings_discovery(self):
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            obj_id = 'frame_tv_art_settings'
            cfg_topic = f"{self.mqtt_discovery_prefix}/sensor/{obj_id}/config"
            device = {
                "identifiers": [f"frame_tv_art_{self.ip}"],
                "name": "Frame TV Art",
                "manufacturer": "Custom",
                "model": "Art Uploader",
            }
            payload = {
                "name": "Frame TV Settings",
                "default_entity_id": "sensor.frame_tv_art_settings",
                "state_topic": self.mqtt_settings_state_topic,
                "json_attributes_topic": self.mqtt_settings_attr_topic,
                "unique_id": obj_id,
                "icon": "mdi:cog",
                "device": device,
                "enabled_by_default": True,
            }
            try:
                self._publish_and_wait(cfg_topic, json.dumps(payload), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)
        except Exception as e:
            self.log.warning('MQTT settings discovery publish failed: %s', e)

    def _publish_settings_state(self):
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            attrs = {
                "SAMSUNG_TV_ART_MAX_UPLOADS": str(os.environ.get('SAMSUNG_TV_ART_MAX_UPLOADS', '30')),
                "SAMSUNG_TV_ART_UPDATE_MINUTES": str(int(max(0, (self.update_time or 0) / 60))),
                "SAMSUNG_TV_ART_TV_IP": str(os.environ.get('SAMSUNG_TV_ART_TV_IP', self.ip or '')),
            }
            try:
                self._publish_and_wait(self.mqtt_settings_state_topic, "online", qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_settings_state_topic, "online", qos=0, retain=True)
            try:
                self._publish_and_wait(self.mqtt_settings_attr_topic, json.dumps(attrs, separators=(",", ":")), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_settings_attr_topic, json.dumps(attrs, separators=(",", ":")), qos=0, retain=True)
        except Exception as e:
            self.log.warning('MQTT settings state publish failed: %s', e)

    def _write_overrides(self, updates: dict) -> bool:
        """Write overrides to /data/overrides.env, merging with existing content."""
        try:
            path = '/data/overrides.env'
            current = {}
            if os.path.isfile(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith('#') or '=' not in line:
                                continue
                            k, v = line.split('=', 1)
                            current[k.strip()] = v.strip()
                except Exception:
                    current = {}
            allowed = {
                'SAMSUNG_TV_ART_MAX_UPLOADS',
                'SAMSUNG_TV_ART_UPDATE_MINUTES',
                'SAMSUNG_TV_ART_TV_IP',
            }
            for k, v in updates.items():
                if k in allowed:
                    current[k] = str(v)
            os.makedirs('/data', exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                for k in sorted(current.keys()):
                    f.write(f"{k}={current[k]}\n")
            return True
        except Exception as e:
            self.log.warning('Failed to write overrides: %s', e)
            return False

    def _publish_collections_state(self):
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            opts = []
            has_label_col = self._csv_headers and (
                'artist_name' in self._csv_headers or 'collection_name' in self._csv_headers
            )
            if self.collections_from_csv and has_label_col and self._csv_headers and 'artwork_dir' in self._csv_headers:
                try:
                    # Build collection label options; prefer collection_name over artist_name when present
                    pairs = set()
                    for row in self._csv_by_file.values():
                        an = (row.get('artist_name') or '').strip()
                        cn = (row.get('collection_name') or '').strip()
                        dn = (row.get('artwork_dir') or '').strip()
                        label = cn if cn else an
                        if label and dn:
                            if not os.path.isdir(os.path.join(self.media_root, dn)):
                                self.log.debug(
                                    'Collections label "%s": artwork_dir "%s" not found under media_root; '
                                    'including in options anyway (dir may not be synced yet)', label, dn
                                )
                            pairs.add(label.replace('_', ' '))
                    opts = sorted(pairs)
                    if not opts:
                        # Fallback to folders if CSV produced nothing usable
                        self.log.info(
                            'No usable labels found in CSV (rows=%d, headers=%s); falling back to folder scan',
                            len(self._csv_by_file), self._csv_headers
                        )
                        opts = self._scan_collections()
                    else:
                        self.log.info('Publishing %d collections from CSV (labels): %s', len(opts), opts)
                except Exception as e:
                    self.log.warning('Failed to derive collection label options from CSV; falling back to folders: %s', e)
                    opts = self._scan_collections()
            else:
                if self.collections_from_csv and self._csv_headers:
                    self.log.info(
                        'collections_from_csv enabled but CSV has no artist_name or collection_name column '
                        '(headers: %s); falling back to folder scan', self._csv_headers
                    )
                opts = self._scan_collections()
            # State: human-friendly count
            try:
                self._publish_and_wait(self.mqtt_collections_state_topic, str(len(opts)), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_collections_state_topic, str(len(opts)), qos=0, retain=True)
            # Attrs: provide options list
            attrs = {"options": opts}
            try:
                self._publish_and_wait(self.mqtt_collections_attr_topic, json.dumps(attrs, separators=(",", ":")), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_collections_attr_topic, json.dumps(attrs, separators=(",", ":")), qos=0, retain=True)
        except Exception as e:
            self.log.warning('MQTT collections state publish failed: %s', e)

    def _maybe_reload_csv_and_publish_collections(self):
        try:
            # Throttle checks
            if self.csv_check_interval <= 0:
                return
            now = time.time()
            if now - self._csv_last_check < self.csv_check_interval:
                return
            self._csv_last_check = now
            if not self.csv_path or not os.path.isfile(self.csv_path):
                return
            current_mtime = None
            try:
                current_mtime = os.path.getmtime(self.csv_path)
            except Exception:
                return
            if self._csv_mtime is None or current_mtime != self._csv_mtime:
                self.log.info('Detected CSV change; reloading metadata and refreshing collections')
                self._load_csv_metadata()
                self._publish_collections_state()
        except Exception as e:
            self.log.debug('CSV reload check skipped due to error: %s', e)

    def _publish_selected_collections_state(self):
        # Mirror selected collections back to the shared state topic
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            # Publish labels (artist_name) when possible so UI shows friendly names
            labels = []
            try:
                rev = getattr(self, '_dir_to_artist', {})
                for d in self.selected_collections:
                    labels.append(rev.get(d, d).replace('_', ' '))
            except Exception:
                labels = [str(x).replace('_', ' ') for x in self.selected_collections]
            # 1) Keep shared selection topic as CSV for Web UI / command flow compatibility
            value = ", ".join(labels)
            try:
                self._publish_and_wait(self.selection_mqtt_topic, value, qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.selection_mqtt_topic, value, qos=1, retain=True)
            # 2) Publish HA-safe selected collections sensor state/attributes
            if len(labels) == 1:
                state_value = labels[0]
            elif len(labels) == 0:
                state_value = "none"
            else:
                state_value = f"{len(labels)} selected"
            self._mqtt.publish(self.mqtt_selected_collections_state_topic, state_value, qos=1, retain=True)
            attrs = {
                "selected_collections": list(self.selected_collections),
                "selected_labels": labels,
                "selected_csv": value,
            }
            self._mqtt.publish(self.mqtt_selected_collections_attr_topic, json.dumps(attrs, separators=(",", ":")), qos=1, retain=True)
        except Exception as e:
            self.log.warning('Failed to publish selected collections state: %s', e)

    def _publish_ack(self, cmd, status='ok', message='', req_id=None):
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            ack = {"cmd": cmd, "status": status}
            if message:
                ack["message"] = message
            if req_id is not None:
                ack["req_id"] = req_id
            ack["selected_collections"] = self.selected_collections
            self._mqtt.publish(f"{self.mqtt_ack_prefix}/{cmd}", json.dumps(ack, separators=(",", ":")), qos=0, retain=False)
        except Exception:
            pass

    def _publish_and_wait(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> bool:
        """Publish with QoS and wait for completion to avoid silent drops."""
        try:
            if not self._mqtt:
                self.log.warning('MQTT publish skipped (client not initialised): %s', topic)
                return False
            info = self._mqtt.publish(topic, payload, qos=qos, retain=retain)
            try:
                # Wait briefly for publish; don't block forever
                info.wait_for_publish(timeout=5.0)
            except Exception:
                pass
            rc = getattr(info, 'rc', 0)
            mid = getattr(info, 'mid', None)
            if rc != 0:
                self.log.warning('MQTT publish failed rc=%s topic=%s', rc, topic)
                return False
            self.log.debug('MQTT publish ok mid=%s topic=%s retain=%s qos=%s', mid, topic, retain, qos)
            return True
        except Exception as e:
            self.log.warning('MQTT publish exception for %s: %s', topic, e)
            return False

    def _handle_mqtt_command(self, subtopic, payload_raw):
        # subtopic examples: 'collections/set', 'collections/add', 'collections/remove', 'collections/clear', 'collections/refresh', 'artwork/set'
        cmd = subtopic.strip()
        req_id = None
        try:
            payload = payload_raw.strip() if isinstance(payload_raw, str) else str(payload_raw)
            data = None
            if payload and payload.startswith('{') and payload.endswith('}'):
                try:
                    data = json.loads(payload)
                except Exception:
                    data = None
            if data and isinstance(data, dict):
                req_id = data.get('req_id')
        except Exception:
            payload = ''
            data = None

        try:
            if cmd == 'collections/set':
                cols = []
                if data and 'collections' in data and isinstance(data['collections'], list):
                    cols = [str(c).strip() for c in data['collections'] if str(c).strip()]
                elif payload:
                    cols = [c.strip() for c in payload.split(',') if c.strip()]
                # Map incoming values to artwork_dir folder names when possible
                mapped = []
                for c in cols:
                    mc = self._map_to_artwork_dir(c)
                    if mc and mc not in mapped:
                        mapped.append(mc)
                self.selected_collections = mapped
                self._pending_selection_change = True
                self._publish_selected_collections_state()
                self._cache_selected_collections()
                self._publish_ack('collections/set', 'ok', 'Collections set', req_id)
                return
            if cmd == 'collections/add':
                col = None
                if data and 'collection' in data:
                    col = str(data['collection']).strip()
                elif payload:
                    col = payload.strip()
                if col:
                    mc = self._map_to_artwork_dir(col) or col
                    if mc not in self.selected_collections:
                        self.selected_collections.append(mc)
                    self._pending_selection_change = True
                    self._publish_selected_collections_state()
                    self._cache_selected_collections()
                    self._publish_ack('collections/add', 'ok', f'Added {mc}', req_id)
                else:
                    self._publish_ack('collections/add', 'error', 'No collection provided', req_id)
                return
            if cmd == 'collections/remove':
                col = None
                if data and 'collection' in data:
                    col = str(data['collection']).strip()
                elif payload:
                    col = payload.strip()
                if col:
                    mc = self._map_to_artwork_dir(col) or col
                    self.selected_collections = [c for c in self.selected_collections if c != mc]
                    self._pending_selection_change = True
                    self._publish_selected_collections_state()
                    self._cache_selected_collections()
                    self._publish_ack('collections/remove', 'ok', f'Removed {mc}', req_id)
                else:
                    self._publish_ack('collections/remove', 'error', 'No collection provided', req_id)
                return
            if cmd == 'collections/clear':
                self.selected_collections = []
                self._pending_selection_change = True
                self._publish_selected_collections_state()
                self._cache_selected_collections()
                self._publish_ack('collections/clear', 'ok', 'Cleared collections', req_id)
                return
            if cmd == 'collections/refresh':
                # Reshuffle uploads for current selection without changing selections
                fut = self._schedule_command_coro(self._do_collections_refresh(req_id=req_id), 'collections/refresh')
                if not fut:
                    self._publish_ack('collections/refresh', 'error', 'Failed to queue refresh task', req_id)
                    return
                # Also refresh discovery/state for UI consistency
                self._publish_collections_discovery()
                self._publish_collections_state()
                self._publish_ack('collections/refresh', 'ok', 'Collections refresh queued', req_id)
                return
            if cmd == 'settings/refresh':
                self._publish_settings_discovery()
                self._publish_settings_state()
                self._publish_ack('settings/refresh', 'ok', 'Settings refreshed', req_id)
                return
            if cmd == 'settings/sync_collections':
                fut = self._schedule_command_coro(self._do_sync_collections(req_id=req_id), 'settings/sync_collections')
                if not fut:
                    self._publish_ack('collections/refresh', 'error', 'Failed to queue update & refresh', req_id)
                    return
                self._publish_ack('collections/refresh', 'queued', 'Update & refresh queued', req_id)
                return
            if cmd == 'artwork/set':
                # Accept either { "path": "Collection/file.jpg" } or a plain string payload
                path = None
                if data and 'path' in data:
                    path = str(data['path']).strip()
                elif payload:
                    path = payload.strip()
                if not path:
                    self._publish_ack('artwork/set', 'error', 'No path provided', req_id)
                    return
                # Normalize to relative path from media_root
                rel_path = path
                if os.path.isabs(path):
                    try:
                        rel_path = os.path.relpath(path, self.media_root)
                    except Exception:
                        pass
                full_path = os.path.join(self.media_root, rel_path)
                if not os.path.isfile(full_path):
                    self._publish_ack('artwork/set', 'error', f'File not found: {rel_path}', req_id)
                    return
                # Upload (if needed) and select immediately
                base_name = os.path.basename(rel_path)
                awaitable = self.upload_files([rel_path])
                # Ensure the coroutine is executed in loop-safe way
                fut = self._schedule_command_coro(self._post_upload_select(awaitable, base_name, req_id), 'artwork/set')
                if not fut:
                    self._publish_ack('artwork/set', 'error', 'Failed to queue artwork upload/select', req_id)
                return
            if cmd == 'settings/set':
                if not isinstance(data, dict):
                    self._publish_ack('settings/set', 'error', 'Invalid JSON', None)
                    return
                updates = {}
                apply_runtime = {}
                try:
                    if 'SAMSUNG_TV_ART_MAX_UPLOADS' in data:
                        updates['SAMSUNG_TV_ART_MAX_UPLOADS'] = str(int(data['SAMSUNG_TV_ART_MAX_UPLOADS']))
                    if 'SAMSUNG_TV_ART_UPDATE_MINUTES' in data:
                        minutes = int(float(data['SAMSUNG_TV_ART_UPDATE_MINUTES']))
                        updates['SAMSUNG_TV_ART_UPDATE_MINUTES'] = str(minutes)
                        apply_runtime['UPDATE_SECONDS'] = max(0, minutes * 60)
                    if 'SAMSUNG_TV_ART_TV_IP' in data:
                        updates['SAMSUNG_TV_ART_TV_IP'] = str(data['SAMSUNG_TV_ART_TV_IP']).strip()
                except Exception:
                    self._publish_ack('settings/set', 'error', 'Validation failed', None)
                    return
                if not updates:
                    self._publish_ack('settings/set', 'error', 'No updates', None)
                    return
                if self._write_overrides(updates):
                    # Update process env for immediate reflect in settings state
                    try:
                        for k, v in updates.items():
                            os.environ[k] = v
                    except Exception:
                        pass
                    # Apply runtime-safe changes without restart
                    try:
                        if 'UPDATE_SECONDS' in apply_runtime:
                            self.update_time = int(apply_runtime['UPDATE_SECONDS'])
                            # Reset slideshow timer so new interval takes effect cleanly
                            self.start = time.time()
                    except Exception:
                        pass
                    self._publish_settings_state()
                    # Indicate if restart is recommended (e.g., TV IP change)
                    msg = 'Settings updated'
                    try:
                        if 'SAMSUNG_TV_ART_TV_IP' in updates and updates['SAMSUNG_TV_ART_TV_IP'] and updates['SAMSUNG_TV_ART_TV_IP'] != str(self.ip):
                            msg += ' (TV IP change will apply after restart)'
                            # Stash new ip for reference; reconnect happens on restart
                            self.ip = updates['SAMSUNG_TV_ART_TV_IP']
                    except Exception:
                        pass
                    self._publish_ack('settings/set', 'ok', msg, None)
                else:
                    self._publish_ack('settings/set', 'error', 'Failed to write overrides', None)
                return
            if cmd == 'settings/restart':
                # Exit to allow container restart policy to restart us
                self._publish_ack('settings/restart', 'ok', 'Restarting', None)
                try:
                    os._exit(0)
                except Exception:
                    pass
                return
            # Unknown command
            self._publish_ack(cmd, 'error', 'Unknown command', req_id)
        except Exception as e:
            self._publish_ack(cmd, 'error', f'Exception: {e}', req_id)

    async def _do_sync_collections(self, req_id=None):
        """Fetch git repos → rebuild CSV → reload metadata → reseed TV.
        All acks go to collections/refresh so the same progress log as Refresh is reused.
        """
        if self._collections_sync_running:
            self.log.info('settings/sync_collections ignored: already running')
            self._publish_ack('collections/refresh', 'error', 'Update already running', req_id)
            return
        self._collections_sync_running = True
        try:
            self.log.info('settings/sync_collections started (req_id=%s)', req_id)
            self._publish_ack('collections/refresh', 'started', 'Fetching latest collection updates...', req_id)

            has_sources = bool(os.environ.get('SAMSUNG_TV_ART_COLLECTIONS')) or os.path.isfile('/data/collections.list')
            if not has_sources:
                self.log.info('settings/sync_collections: no git sources configured; reseeding from local files only')
            else:
                fetch_ok = True
                try:
                    self._publish_ack('collections/refresh', 'progress', 'Fetching from git repositories...', req_id)
                    proc = await asyncio.create_subprocess_exec(
                        '/bin/sh', '-c', 'chmod +x /app/scripts/fetch_collections.sh 2>/dev/null || true; /app/scripts/fetch_collections.sh',
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _out, err = await proc.communicate()
                    if proc.returncode != 0:
                        fetch_ok = False
                        self.log.warning('On-demand collections fetch failed rc=%s err=%s', proc.returncode, (err or b'').decode('utf-8', errors='ignore')[:400])
                except Exception as e:
                    fetch_ok = False
                    self.log.warning('On-demand collections fetch exception: %s', e)

                if not fetch_ok:
                    self.log.warning('settings/sync_collections failed during fetch')
                    self._publish_ack('collections/refresh', 'error', 'Git fetch failed — check container logs', req_id)
                    return

                csv_ok = True
                try:
                    self._publish_ack('collections/refresh', 'progress', 'Rebuilding artwork database from CSV...', req_id)
                    proc2 = await asyncio.create_subprocess_exec(
                        'python', '/app/scripts/aggregate_csv.py', '/app/frame_tv_art_collections', '/app/artwork_data.csv',
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _out2, err2 = await proc2.communicate()
                    if proc2.returncode != 0:
                        csv_ok = False
                        self.log.warning('On-demand CSV aggregate failed rc=%s err=%s', proc2.returncode, (err2 or b'').decode('utf-8', errors='ignore')[:400])
                except Exception as e:
                    csv_ok = False
                    self.log.warning('On-demand CSV aggregate exception: %s', e)

                if not csv_ok:
                    self.log.warning('settings/sync_collections failed during csv aggregation')
                    self._publish_ack('collections/refresh', 'error', 'Git fetch done — CSV rebuild failed', req_id)
                    return

                try:
                    self._publish_ack('collections/refresh', 'progress', 'Reloading collection metadata...', req_id)
                    self._load_csv_metadata()
                except Exception:
                    pass
                try:
                    self._publish_collections_state()
                    self._publish_selected_collections_state()
                    self._publish_settings_state()
                except Exception:
                    pass

            self.log.info('settings/sync_collections proceeding to TV reseed (req_id=%s)', req_id)
            await self._do_full_reseed(req_id=req_id, skip_started_ack=True)
        except Exception as e:
            self.log.warning('settings/sync_collections exception: %s', e)
            self._publish_ack('collections/refresh', 'error', f'Exception: {e}', req_id)
        finally:
            self._collections_sync_running = False

    async def _post_upload_select(self, upload_coro, base_name, req_id):
        try:
            await upload_coro
            # Select the just-uploaded image
            content_id = None
            for k, v in self.uploaded_files.items():
                if k == base_name:
                    content_id = v.get('content_id')
                    break
            if content_id:
                await self.tv.select_image(content_id)
                self.current_content_id = content_id
                await self.update_ha_selected_artwork(content_id)
                self._publish_ack('artwork/set', 'ok', f'Selected {base_name}', req_id)
            else:
                self._publish_ack('artwork/set', 'error', f'Upload failed for {base_name}', req_id)
        except Exception as e:
            self._publish_ack('artwork/set', 'error', f'Exception selecting: {e}', req_id)

    def _load_csv_metadata(self):
        """Load artwork CSV into memory for attribute publishing. Optional."""
        try:
            if not self.csv_path or not os.path.isfile(self.csv_path):
                self.log.info('CSV metadata not found at %s; attributes will be minimal', self.csv_path)
                return
            with open(self.csv_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                self._csv_headers = list(reader.fieldnames or [])
                self._csv_by_file = {}
                # Rebuild artist<->dir maps
                self._artist_to_dir = {}
                self._dir_to_artist = {}
                for row in reader:
                    key = (row.get('artwork_file') or '').strip()
                    if key:
                        self._csv_by_file[key] = row
                    # Build bidirectional mapping when columns exist
                    try:
                        an = (row.get('artist_name') or '').strip()
                        cn = (row.get('collection_name') or '').strip()
                        dn = (row.get('artwork_dir') or '').strip()
                        if an and dn:
                            # Keep first-seen mapping to be stable
                            if an not in self._artist_to_dir:
                                self._artist_to_dir[an] = dn
                                spaced = an.replace('_', ' ')
                                if spaced and spaced not in self._artist_to_dir:
                                    self._artist_to_dir[spaced] = dn
                                n1 = self._normalize_collection_key(an)
                                if n1 and n1 not in self._artist_to_dir:
                                    self._artist_to_dir[n1] = dn
                                n2 = self._normalize_collection_key(spaced)
                                if n2 and n2 not in self._artist_to_dir:
                                    self._artist_to_dir[n2] = dn
                            # Also map collection_name variants -> artwork_dir
                            if cn and cn not in self._artist_to_dir:
                                self._artist_to_dir[cn] = dn
                                cn_spaced = cn.replace('_', ' ')
                                if cn_spaced and cn_spaced not in self._artist_to_dir:
                                    self._artist_to_dir[cn_spaced] = dn
                                cn_n1 = self._normalize_collection_key(cn)
                                if cn_n1 and cn_n1 not in self._artist_to_dir:
                                    self._artist_to_dir[cn_n1] = dn
                            # Prefer collection_name as the display label; fall back to artist_name
                            if dn not in self._dir_to_artist:
                                label = cn if cn else an
                                self._dir_to_artist[dn] = label.replace('_', ' ')
                    except Exception:
                        pass
            try:
                self._csv_mtime = os.path.getmtime(self.csv_path)
            except Exception:
                self._csv_mtime = None
            self.log.info('Loaded CSV metadata: %d rows, %d headers', len(self._csv_by_file), len(self._csv_headers))
        except Exception as e:
            self.log.warning('Failed to load CSV metadata from %s: %s', self.csv_path, e)

    def next_value(self, value, lst):
        '''
        get next value from list, or return first element
        return None if list is empty
        '''
        return lst[(lst.index(value)+1) % len(lst)] if value in lst else lst[0] if lst else None
        
    async def change_art(self):
        '''
        update displayed art on tv, it next_art is a different content_id to current
        '''
        content_id = self.get_next_art()
        if content_id and content_id != self.current_content_id:
            self.log.info('selecting tv art: content_id: %s (shown %d/%d)', content_id, len(self.shown_content_ids) + 1, len(self.get_content_ids()))
            await self.tv.select_image(content_id)
            self.shown_content_ids.add(content_id)  # Mark as shown
            self.current_content_id = content_id
            await self.update_ha_selected_artwork(content_id)
        else:
            self.log.info('skipping art update, as new content_id: %s is the same', content_id)
    
    async def check_dir(self):
        '''
        scan folder for new, deleted or updated files, but only when tv is in art mode
        '''
        if self._refresh_in_progress:
            return
        try:
            # Refresh CSV-driven collections periodically without needing a restart
            self._maybe_reload_csv_and_publish_collections()
            selection_changed = self.apply_selection()
            update_due = self.update_time > 0 and (time.time() - self.start >= self.update_time)
            if self.selection_only and not selection_changed and not update_due:
                return
            artmode_due = self.artmode_refresh_seconds > 0 and (time.time() - self.last_artmode_check >= self.artmode_refresh_seconds)
            if not selection_changed and not update_due and not artmode_due:
                self.log.debug('No selection change, update due, or artmode refresh; skipping TV poll')
                return
            if not selection_changed and not update_due and artmode_due:
                await self.safe_in_artmode()
                return
            if await self.safe_in_artmode():
                if selection_changed:
                    self.log.info('selection changed, syncing directory: {}'.format(self.folder))
                    self._mirror_prune_to_selected()
                    await self._do_full_reseed()
                # update tv art if enabled by timer
                elif update_due:
                    await self.update_art_timer()
                elif len(self.get_content_ids()) == 1:
                    await self.change_art()
            else:
                self.log.info('artmode or tv is off')
        except Exception as e:
            self.log.warning('error in check_dir, attempting reconnect: %s', e)
            await self.reconnect_tv()

    async def select_artwork(self):
        '''
        main loop
        initialize, check directory for changed files and update
        '''
        await self.initialize()
        while True:
            if not await self.safe_in_artmode():
                backoff_delay = self.get_backoff_delay()
                self.log.info('TV not in art mode; pausing %d seconds (backoff level %d)', backoff_delay, self.consecutive_failures)
                await asyncio.sleep(backoff_delay)
                continue
            await self.check_dir()
            # Periodically republish current artwork state to keep MQTT fresh
            try:
                if self.state_refresh_seconds > 0:
                    now = time.time()
                    if now - self._last_state_publish >= self.state_refresh_seconds:
                        await self._publish_current_artwork_state(force=False)
                        self._last_state_publish = now
            except Exception:
                pass
            if self.period == 0:
                break
            await asyncio.sleep(self.period)

    async def _publish_current_artwork_state(self, force=False):
        """Poll current TV artwork and publish MQTT state/attributes.
        Uses uploaded_files mapping to derive filename when possible.
        """
        if self._refresh_in_progress:
            return
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            cid = await self.get_current_artwork()
        except Exception:
            cid = None
        # If nothing has changed and not forced, skip
        if cid == self.current_content_id and not force:
            # Still ensure attributes are up to date periodically
            pass
        else:
            self.current_content_id = cid
        # Derive filename and collection if known
        filename = self.get_filename_for_content_id(self.current_content_id) if self.current_content_id else None
        display = None
        collection = None
        if filename:
            display = os.path.splitext(filename)[0]
            # Try to infer collection from cached metadata
            try:
                rec = self.uploaded_files.get(filename, {})
                rel_path = rec.get('path_rel')
                if rel_path:
                    parts = os.path.normpath(rel_path).split(os.sep)
                    if parts:
                        collection = parts[0]
            except Exception:
                collection = None
        else:
            # Unknown content (e.g., selected outside uploader); publish sentinel values
            display = 'Unknown' if self.current_content_id else 'Standby'
        try:
            self._publish_mqtt_discovery()
            self._publish_mqtt_state(display, filename or '', collection)
        except Exception:
            pass

    async def _do_full_reseed(self, req_id=None, skip_started_ack=False):
        """Standby → delete all TV uploads → upload fresh randomized set → display first.
        Shared by the Refresh button, collection selection changes, and Update & Refresh.
        Always publishes MQTT ack progress messages so both UIs show progress for any
        trigger (button press, collection selection change, startup seeding, etc.).
        When skip_started_ack is True, skips the 'started' ack (caller already sent one).
        """
        # Always generate a req_id so acks are published regardless of how we were called.
        # Auto-triggered reseeds (selection change, startup) get a synthetic id.
        if req_id is None:
            req_id = f'auto_{int(time.time() * 1000)}'

        def ack(status, msg):
            self._publish_ack('collections/refresh', status, msg, req_id)

        self._refresh_in_progress = True
        try:
            if skip_started_ack:
                ack('progress', 'Preparing TV for update — switching to standby...')
            else:
                ack('started', 'Preparing refresh — switching TV to standby...')
            await self.ensure_standby_selected()
            if self.standby_content_id:
                try:
                    await self.tv.select_image(self.standby_content_id)
                    self._publish_mqtt_state('Standby', 'standby.png', None)
                    self.log.info('Standby selected before cleanup: %s', self.standby_content_id)
                except Exception as e:
                    self.log.warning('Failed to select standby before cleanup: %s', e)

            ack('progress', 'Removing old uploads from TV...')
            await self.cleanup_old_uploads()

            ack('progress', 'Uploading new artwork to TV...')
            await self.sync_file_list()
            files_added = await self.add_files([])

            if files_added and len(self.get_content_ids()) > 0:
                self.log.info('Uploads complete, displaying first artwork')
                await self.change_art()
                self.start = time.time()
                self.write_program_data()
                ack('ok', f'Refresh complete — {files_added} photos loaded')
            else:
                ack('ok', 'Refresh completed')
        except Exception as e:
            self.log.warning('Error in full reseed: %s', e)
            ack('error', f'Exception: {e}')
            raise
        finally:
            self._refresh_in_progress = False

    async def _do_collections_refresh(self, req_id=None):
        """MQTT-triggered refresh: prunes mirror then reseeds with ack progress messages."""
        self._mirror_prune_to_selected()
        await self._do_full_reseed(req_id=req_id)
            
async def main():
    global log
    log = logging.getLogger('Main')
    args = parseargs()
    log.info('Program Started')
    if args.debug:
        log.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
    log.debug('Debug mode')
    
    args.folder = os.path.normpath(args.folder)
    
    if not os.path.exists(args.folder):
        log.warning('folder {} does not exist, exiting'.format(args.folder))
        os._exit(1)
    
    # Retry initialization with backoff to avoid hammering TV on startup
    max_retries = 10
    retry_delay = 30  # Start with 30 seconds
    
    for attempt in range(max_retries):
        try:
            mon = monitor_and_display(  args.ip,
                                        args.folder,
                                        period          = args.check,
                                        update_time     = args.update,
                                        include_fav     = args.favourite,
                                        sync            = args.sync,
                                        matte           = args.matte,
                                        sequential      = args.sequential,
                                        on              = args.on,
                                        token_file      = args.token_file,
                                        exclude         = args.exclude,
                                        exclude_content_ids = args.exclude_content_ids,
                                        standby         = args.standby)
            await mon.start_monitoring()
            break  # Success, exit retry loop
        except Exception as e:
            log.warning('Failed to connect to TV (attempt %d/%d): %s', attempt + 1, max_retries, e)
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** min(attempt, 4))  # Cap at 16x
                log.info('Waiting %d seconds before retry...', wait_time)
                await asyncio.sleep(wait_time)
            else:
                log.error('Max retries reached, giving up')
                os._exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        os._exit(1)
