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
import threading
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
import urllib.request
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

# from samsungframetv.async_art import SamsungTVAsyncArt  # Moved inside class
# from samsungframetv import __version__  # Not used

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    mqtt = None

logging.basicConfig(level=logging.INFO)



class MQTTLogHandler(logging.Handler):
    """Logging handler that forwards records to frame_tv/log (non-retained, QoS 0)."""
    TOPIC = 'frame_tv/log'

    def __init__(self, mqtt_client):
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter('%(levelname)s:%(message)s'))
        self._mqtt = mqtt_client
        self._publishing = False  # re-entrancy guard

    def emit(self, record):
        if self._publishing:
            return
        try:
            self._publishing = True
            msg = self.format(record)
            self._mqtt.publish(self.TOPIC, msg, qos=0, retain=False)
        except Exception:
            pass
        finally:
            self._publishing = False


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

        Thumbnail fetches are best-effort: some legacy Frame TVs (e.g. Art API 1.07)
        return raw binary JPEG through a UTF-8/JSON decode path inside samsungtvws,
        which raises UnicodeDecodeError. We skip the offending thumbnail(s) instead of
        aborting the whole sync — otherwise the exception tears down the connection and
        can trigger a reconnect loop after uploads exist on the TV.
        '''
        thumbnails = {}
        if content_ids:
            if self.mon.api_version == 0:
                for content_id in content_ids:
                    try:
                        thumbnails[content_id] = await self.mon.tv.get_thumbnail(content_id)
                    except Exception as e:
                        self.log.warning('skipping thumbnail %s — could not fetch/decode: %s', content_id, e)
            elif self.mon.api_version == 1:
                try:
                    thumbnails = {os.path.splitext(k)[0]:v for k,v in (await self.mon.tv.get_thumbnail_list(content_ids)).items()}
                except Exception as e:
                    self.log.warning('failed to fetch thumbnail list (%s) — skipping thumbnail sync', e)
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
        diff = sum(img3.get_flattened_data())/(384*216)  #normalize
        equal_content = diff <= 1.0                 #pick a threshhold
        self.log.debug('equal_content: {}, diff: {}'.format(equal_content, diff))
        return equal_content


class _MatteRejectedError(Exception):
    '''Raised when the TV rejects a matte for an image during apply (error -7).
    Carries the rejected matte id so the caller can surface a friendly message.'''
    def __init__(self, matte):
        super().__init__(f'TV rejected matte {matte!r} (-7)')
        self.matte = matte


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
        self.artmode_refresh_seconds = int(os.environ.get('SAMSUNG_TV_ART_MODE_CHECK_SECONDS', '1'))
        self.last_artmode_check = 0
        self.consecutive_failures = 0
        self.max_backoff_seconds = 15  # Max 15 seconds between art mode re-checks
        self.reconnect_delay = 5
        self.update_time = int(max(0, update_time*60))   #convert minutes to seconds
        self.period = min(max(5, period), self.update_time) if self.update_time > 0 else period
        self.include_fav = include_fav
        self.sync = sync
        self.matte = matte
        self.sequential = sequential
        # Allow SAMSUNG_TV_ART_SEQUENTIAL env var to override CLI arg so it persists across restarts
        _env_seq = os.environ.get('SAMSUNG_TV_ART_SEQUENTIAL', '').lower()
        if _env_seq in ('1', 'true', 'yes'):
            self.sequential = True
        elif _env_seq in ('0', 'false', 'no'):
            self.sequential = False
        # Slideshow override (persisted to /data/slideshow_override.json)
        self.slideshow_override = None  # None = auto; list[str] of path_rel = manual override
        self.slideshow_override_path = '/data/slideshow_override.json'
        self.on = on
        self.exclude = exclude
        self.exclude_content_ids = exclude_content_ids
        self.standby = standby
        self.standby_content_id = None
        self.standby_image_date = None     # TV-assigned image_date of the standby upload; used to detect content-id reuse
        # Autosave token to file
        self.token_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), token_file) if token_file else token_file
        self.program_data_path = './uploaded_files.json'
        self.uploaded_files = {}
        self.fav = set()
        self.api_version = 0
        self.api_version_str = None        # Raw string from get_api_version(), e.g. '0.97' or '4.3.4.0'
        self.api_version_failed = False    # True when api_version request itself errors (e.g. error -9)
        self._ws_binary_latched = False    # True once a WS-binary upload has succeeded via -1 fallback
        self._upload_compat_warned = False  # Emit old-API diagnostic at most once
        self.start = time.time()
        self.current_content_id = None
        self.shown_content_ids = set()  # Track shown images for shuffle-without-repeat
        self.pil = PIL_methods(self)
        self.tv = None  # Defer TV connection until start_monitoring
        # Rate limits (configurable)
        self.upload_delay_seconds = int(os.environ.get('SAMSUNG_TV_ART_UPLOAD_DELAY_SECONDS', '1'))
        self.delete_delay_seconds = int(os.environ.get('SAMSUNG_TV_ART_DELETE_DELAY_SECONDS', '1'))
        self.post_delete_recovery_seconds = int(os.environ.get('SAMSUNG_TV_ART_POST_DELETE_RECOVERY_SECONDS', '5'))
        self.max_uploads = int(os.environ.get('SAMSUNG_TV_ART_MAX_UPLOADS', '10'))
        # MQTT configuration (optional)
        # mqtt_enabled: True when MQTT_HOST is explicitly configured, OR MQTT_DISCOVERY is set.
        # Previously gated only on MQTT_DISCOVERY, which silently disabled all MQTT for users
        # who configured MQTT_HOST but didn't need HA discovery.
        _mqtt_host_set = bool(os.environ.get('SAMSUNG_TV_ART_MQTT_HOST'))
        _mqtt_discovery_val = os.environ.get('SAMSUNG_TV_ART_MQTT_DISCOVERY', '').lower()
        self.mqtt_discovery = _mqtt_discovery_val in ['1', 'true', 'yes']
        self.mqtt_enabled = _mqtt_host_set or self.mqtt_discovery
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
        # Slideshow state/available topics
        self.mqtt_slideshow_state_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SLIDESHOW_STATE', 'frame_tv/slideshow/state')
        self.mqtt_slideshow_attr_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SLIDESHOW_ATTR', 'frame_tv/slideshow/attributes')
        self.mqtt_slideshow_available_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SLIDESHOW_AVAILABLE', 'frame_tv/slideshow/available')
        self.mqtt_slideshow_presets_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SLIDESHOW_PRESETS', 'frame_tv/slideshow/presets')
        self.slideshow_presets_path = '/data/slideshow_presets.json'
        self._slideshow_presets = []  # in-memory cache, source of truth for republish
        # Per-image matte overrides — stored {path_rel: matte_id}
        self.mqtt_slideshow_mattes_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SLIDESHOW_MATTES', 'frame_tv/slideshow/mattes')
        self.mqtt_slideshow_matte_options_topic = os.environ.get('SAMSUNG_TV_ART_MQTT_SLIDESHOW_MATTE_OPTIONS', 'frame_tv/slideshow/matte_options')
        self.matte_overrides_path = '/data/matte_overrides.json'
        self._matte_overrides = {}
        self._matte_options_cache = None  # {'matte_types':[...], 'matte_colors':[...]}
        # Matte support is reported by the TV via get_matte_list() (published
        # to MQTT on demand). The TV lists EVERY layout it supports, not just
        # the ones that will succeed for a given image — Samsung's own app
        # decides per-image at render time, which is too expensive for us to
        # replicate. Instead we surface every option and react to the TV's
        # error -7 at apply time (revert + warn).
        self.ha_rest_enabled = False  # REST disabled in MQTT-only build
        self._mqtt = None
        self._presets_from_broker = False   # True once retained presets msg received
        self._presets_bootstrap_timer = None
        self._mqtt_config_published = False
        # CSV metadata (optional)
        self.csv_path = os.environ.get('SAMSUNG_TV_ART_CSV_PATH', '/app/artwork_data.csv')
        self._csv_headers = []
        self._csv_by_file = {}
        self._csv_by_path = {}  # keyed by artwork_dir/artwork_file — avoids filename collisions across collections
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
        self._startup_in_progress = False  # True between MQTT init and end of initialize()
        self._in_art_mode = None           # None = unknown, True/False = last known state
        self._last_slideshow_paths = set() # path_rel values from the previous seed, used to avoid re-picking the same images
        self._loop = None
        self._collections_sync_running = False
        self._artmode_event = None         # asyncio.Event set when TV signals an art mode change
        self._not_in_artmode_logged = False    # suppress repeated 'not in art mode' messages
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
            dirs = self._scan_collections()
            for d in dirs:
                if self._normalize_collection_key(d) == target:
                    return d
            # For subdirectory collections (e.g. "Artists/Kelly_Burns"), also try matching
            # against just the leaf name so a bare label like "Kelly Burns" resolves correctly.
            for d in dirs:
                if self._normalize_collection_key(os.path.basename(d)) == target:
                    return d
            # common fallback: spaces in labels vs underscores on disk
            underscored = target.replace(' ', '_')
            for d in dirs:
                if d.lower() == underscored.lower():
                    return d
                if os.path.basename(d).lower() == underscored.lower():
                    return d
        except Exception:
            pass
        return None
    
    def _create_tv_connection(self):
        """Create TV connection object. May raise if TV is unreachable."""
        import socket
        try:
            from samsungframetv.async_art import SamsungTVAsyncArt
        except ImportError:
            # Images built before the upstream rename (library < 4.0.0) ship it as samsungtvws
            from samsungtvws.async_art import SamsungTVAsyncArt
        device_name = os.environ.get('SAMSUNG_TV_ART_DEVICE_NAME') or socket.gethostname() or 'SamsungTvRemote'
        # Port 8002 is the SSL WebSocket (default for most models). Some Tizen 9.0
        # firmwares (Frame 2024+) filter 8002 and require the plain port 8001.
        tv_port = int(os.environ.get('SAMSUNG_TV_ART_PORT', '8002'))
        self.tv = SamsungTVAsyncArt(host=self.ip, port=tv_port, token_file=self.token_file, name=device_name)
        self._artmode_event = asyncio.Event()
        self._register_artmode_callbacks()

    def _register_artmode_callbacks(self):
        """Register WebSocket event callbacks so art mode changes wake the main loop instantly."""
        def _signal_artmode_change():
            if self._artmode_event:
                self._artmode_event.set()

        async def _on_go_to_standby(event, response):
            self.log.debug('TV WebSocket event: go_to_standby')
            # Don't update _in_art_mode here — let safe_in_artmode() own all state
            # transitions so MQTT publishing logic fires correctly.
            _signal_artmode_change()

        async def _on_art_mode_changed(event, response):
            try:
                data = json.loads(response['data'])
                new_state = data.get('status') == 'on'
            except Exception:
                new_state = None
            self.log.debug('TV WebSocket event: art_mode_changed (status=%s)', new_state)
            # Don't write _in_art_mode directly — just wake the loop.
            _signal_artmode_change()

        async def _on_wakeup(event, response):
            self.log.debug('TV WebSocket event: wakeup')
            _signal_artmode_change()

        try:
            self.tv.callbacks['go_to_standby'] = _on_go_to_standby
            self.tv.callbacks['art_mode_changed'] = _on_art_mode_changed
            self.tv.callbacks['wakeup'] = _on_wakeup
        except Exception as e:
            self.log.warning('Failed to register TV artmode callbacks: %s', e)
        
    async def start_monitoring(self):
        '''
        program entry point
        '''
        try:
            self._loop = asyncio.get_running_loop()
        except Exception:
            self._loop = None
        # Create TV connection (may raise if TV offline)
        try:
            self._create_tv_connection()
        except Exception as e:
            self.log.warning('TV unavailable at startup — MQTT/web UI will run without TV (retry on reconnect): %s', e)
        # Ensure CSV metadata is loaded before MQTT and selection logic.
        if self.require_csv_on_start:
            ready = await self._wait_for_csv_metadata()
            if not ready:
                raise RuntimeError(f'CSV metadata unavailable at startup: {self.csv_path}')
        else:
            self._load_csv_metadata()
        # Load persisted slideshow override before MQTT so state is ready when topics publish
        self._load_slideshow_override()
        self._load_slideshow_presets()
        self._load_matte_overrides()
        # Init MQTT if enabled
        if self.mqtt_enabled:
            self._init_mqtt()
            # Lock the UI as early as possible.  TV connect + standby + (optional)
            # matte probe can take many seconds; without an early uploading=true
            # signal, the web UI shows slideshow controls as available during
            # this window (driven by the previous session's retained state).
            # We publish discovery + slideshow_state now so the grid locks
            # immediately on (re)connect.  These are re-published below once the
            # full discovery suite runs; the duplicate is harmless.
            try:
                self._startup_in_progress = True
                self._publish_mqtt_discovery()
                self._publish_slideshow_state()
            except Exception:
                pass
        # Start periodic memory logging if enabled
        try:
            if getattr(self, 'memlog_seconds', 0) > 0:
                asyncio.create_task(self._memlogger())
        except Exception:
            pass

        if self.on and self.tv is not None and not await self.tv.on():
            self.log.info('TV is off, exiting')
        else:
            self.log.info('Start Monitoring')
            if self.tv is not None:
                try:
                    # Use a longer timeout when no token exists (first-time pairing) so the
                    # user has time to see and accept the pairing prompt on the TV.
                    # An empty token file counts as "no token" — the TV will prompt for
                    # approval and the handshake needs the full window, otherwise it
                    # times out (ms.channel.timeOut) and re-prompts on every reconnect.
                    needs_pairing = (
                        not self.token_file
                        or not os.path.exists(self.token_file)
                        or os.path.getsize(self.token_file) == 0
                    )
                    connect_timeout = 120 if needs_pairing else 15
                    if needs_pairing:
                        self.log.info('No saved token — connecting (up to %ds); the TV may show a one-time pairing prompt', connect_timeout)
                    await asyncio.wait_for(self.tv.start_listening(), timeout=connect_timeout)
                    self.log.info('Started')
                except asyncio.TimeoutError:
                    self.log.warning('TV connection timed out at startup — will retry in main loop')
                except Exception as e:
                    self.log.error('failed to connect with TV: {}'.format(e))
                if self.tv.is_alive():
                    try:
                        # Determine the Art API version before the first upload so the
                        # standby image is sent over the correct transport. Legacy Frame
                        # TVs (e.g. Art API 0.97/1.07) need the WS-binary path, and without
                        # this the initial ensure_standby_selected() would fail with error -1
                        # before the version was known.
                        await self.get_api_version()
                        await self.check_matte()
                        await self.ensure_standby_selected()
                        await self.cleanup_old_uploads()
                    except Exception as e:
                        self.log.warning('Startup TV setup error (non-fatal): %s', e)
            # Always run select_artwork — even if TV is not reachable right now,
            # the main loop will wait for art mode in the meantime.
            try:
                if self.mqtt_enabled:
                    self._publish_mqtt_discovery()
                    self._publish_collections_discovery()
                    self._publish_settings_discovery()
                    await asyncio.sleep(0)
                    self._publish_collections_state()
                    self._publish_settings_state()
                    self._startup_in_progress = True
                    self._publish_slideshow_state()
                    self._publish_slideshow_available()
                    # If TV is offline, clear any stale retained artwork state so the
                    # web UI doesn't show "in art mode" from a previous session.
                    if self.tv is None:
                        self._in_art_mode = False
                        self._publish_mqtt_state('Unavailable', 'unavailable', None)
                await self.select_artwork()
            finally:
                if self.tv is not None:
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
        # If TV connection object was never created (e.g. TV unreachable at startup),
        # try to create it now before attempting start_listening.
        if self.tv is None:
            try:
                self._create_tv_connection()
            except Exception as e:
                self.log.debug('TV still unavailable during reconnect: %s', e)
                return False
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

    async def _query_artmode(self):
        """Determine Art Mode, resilient to legacy Frame TVs.

        Tries tv.in_artmode() first; if it reports false OR raises, falls back to an
        explicit get_artmode() request (with one retry). get_artmode() doesn't depend
        on the REST PowerState field that legacy models (e.g. 17_KANTM_UHD, Art API
        1.07) omit — those make tv.on() False and tv.in_artmode() False even in Art
        Mode. Raises if the TV can't be queried at all, so callers can apply backoff.
        """
        try:
            if await self.tv.in_artmode():
                return True
        except Exception as e:
            self.log.debug('in_artmode() failed, falling back to get_artmode(): %s', e)
        last_err = None
        for attempt in range(2):
            try:
                return str(await self.tv.get_artmode()).lower() == 'on'
            except Exception as e:
                last_err = e
                self.log.debug('get_artmode() attempt %d failed: %s', attempt + 1, e)
        raise last_err if last_err else AssertionError('artmode query failed')

    async def safe_in_artmode(self):
        """Return True if TV reports art mode; False on any error. Uses exponential backoff."""
        try:
            self.last_artmode_check = time.time()
            in_artmode = await self._query_artmode()
            # Success - reset failure counter
            self.consecutive_failures = 0
            prev = self._in_art_mode
            self._in_art_mode = bool(in_artmode)
            if prev is True and not in_artmode:
                # TV just left art mode — publish sentinel with in_art_mode: False so UIs disable
                self._publish_mqtt_state('Unavailable', 'unavailable', None)
            elif prev is not True and in_artmode:
                # TV just (re-)entered art mode, or this is the first confirmed
                # True after startup (prev=None).  Immediately republish so UIs
                # re-enable and any stale retained in_art_mode:false from a
                # previous session is cleared.
                # Set _last_state_publish=0 AND do an immediate forced publish so the
                # in_art_mode: True state is pushed to MQTT now, even if a reseed starts
                # right after (which would set _refresh_in_progress=True and block the
                # next periodic publish for the entire reseed duration).
                self._last_state_publish = 0
                if not self._refresh_in_progress:
                    try:
                        await self._publish_current_artwork_state(force=True)
                    except Exception:
                        pass
            return in_artmode
        except AssertionError:
            self.consecutive_failures += 1
            log_fn = self.log.warning if self.consecutive_failures == 1 else self.log.debug
            log_fn('TV artmode check failed (empty response, failure %d); treating as off', self.consecutive_failures)
            prev = self._in_art_mode
            self._in_art_mode = False
            if prev is True:
                self._publish_mqtt_state('Unavailable', 'unavailable', None)
            return False
        except Exception as e:
            self.consecutive_failures += 1
            log_fn = self.log.warning if self.consecutive_failures == 1 else self.log.debug
            log_fn('TV artmode check failed (failure %d): %s', self.consecutive_failures, e)
            prev = self._in_art_mode
            self._in_art_mode = False
            if prev is True:
                self._publish_mqtt_state('Unavailable', 'unavailable', None)
            return False

    def get_backoff_delay(self):
        """Calculate exponential backoff delay based on consecutive failures."""
        if self.consecutive_failures <= 1:
            return self.artmode_refresh_seconds or 1
        # Exponential backoff: 1, 2, 4... (capped at max_backoff_seconds)
        delay = min(1 * (2 ** (self.consecutive_failures - 1)), self.max_backoff_seconds)
        return delay

    async def cleanup_old_uploads(self):
        """Delete previously uploaded photos from the TV in small batches to avoid overwhelming it."""
        try:
            if not await self.safe_in_artmode():
                self.log.info('TV not in art mode, skipping cleanup')
                return

            # Protect the currently-displayed image from deletion even if standby
            # selection hasn't taken effect yet (e.g. slow TV or stale content_id).
            currently_displayed = await self.get_current_artwork()

            my_photos = await self.get_tv_content('MY-C0002')
            if my_photos:
                skip = {cid for cid in (self.standby_content_id, currently_displayed) if cid}
                my_photos = [cid for cid in my_photos if cid not in skip]
                if currently_displayed and currently_displayed != self.standby_content_id:
                    self.log.info('Protecting currently-displayed image from deletion: %s', currently_displayed)
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
                    await asyncio.sleep(self.post_delete_recovery_seconds)
            self.cache = {}
            self.current_key = None
            self.uploaded_files = {}  # cleared from TV; force clean slate for add_files headroom
            try:
                if os.path.isfile(self.cache_path):
                    os.remove(self.cache_path)
            except Exception as e:
                self.log.warning('Failed to remove cache file: %s', e)
            # The standby image is protected from deletion above, so its content id is
            # still valid on the TV. Re-persist it after the cache wipe so standby
            # dedup survives — otherwise the id written by ensure_standby_selected()
            # (which runs just before cleanup) is lost and standby is re-uploaded on
            # every restart.
            if self.standby_content_id:
                self._cache_standby_content_id(self.standby_content_id, self.standby_image_date)
        except Exception as e:
            self.log.warning('Failed to cleanup uploads: %s', e)

    async def ensure_standby_selected(self):
        """Upload and select standby image if present.
        Idempotent: skips the upload when the standby is already active in this session,
        but first verifies the cached standby content_id still exists on the TV. The Frame
        can drop/renumber MY_F#### ids (purge, reboot, content-id reuse), which would leave
        a stale id that now resolves to a random leftover artwork; when that happens we clear
        the id and re-upload a fresh standby so 'standby' always maps to standby.png.
        """
        if not self.standby:
            return
        standby_path = self.standby if os.path.isabs(self.standby) else os.path.join(self.media_root, self.standby)
        if not os.path.isfile(standby_path):
            self.log.warning('Standby file not found on disk: %s', standby_path)
            return
        # Restore the standby id from the persistent cache so we don't re-upload a
        # fresh standby on every restart (the id lives only in memory otherwise).
        if not self.standby_content_id:
            self.standby_content_id, self.standby_image_date = self._load_standby_content_id()
        if self.standby_content_id:
            # Validate the cached id against the TV's live My-Collection list. Match on
            # both content_id AND image_date: the Frame can free and later reuse a
            # MY_F#### id for a different upload, so a present id alone isn't proof it's
            # still our standby. Only invalidate on a definitive mismatch — a None list
            # means the query failed and we must not blow away a still-valid id.
            entries = await self.get_tv_content_entries('MY-C0002')
            if entries is not None:
                match = next((e for e in entries if e.get('content_id') == self.standby_content_id), None)
                reused = bool(match and self.standby_image_date and match.get('image_date')
                              and match.get('image_date') != self.standby_image_date)
                if match is None or reused:
                    reason = 'content-id reused by a different image' if reused else 'no longer on the TV'
                    self.log.warning('Cached standby id %s is stale (%s) — re-uploading standby', self.standby_content_id, reason)
                    self.standby_content_id = None
                    self.standby_image_date = None
                    self._cache_standby_content_id(None, None)
                else:
                    self.log.debug('Standby already active as %s; skipping re-upload', self.standby_content_id)
                    return
            else:
                self.log.debug('Standby id %s could not be validated (TV query failed); skipping re-upload', self.standby_content_id)
                return
        try:
            file_data, file_type = self.read_file(standby_path)
            if file_data and self.tv.art_mode:
                # self.tv.art_mode is True here (last-known WebSocket state), so we
                # know the TV is in art mode.  Record this immediately so the
                # _publish_mqtt_state below carries in_art_mode: true instead of
                # the stale bool(None)=false that would otherwise be retained
                # until safe_in_artmode() finally succeeds.
                self._in_art_mode = True
                content_id = await self._upload_to_tv(file_data, file_type, self.matte)
                if content_id:
                    self.standby_content_id = content_id
                    # Capture the TV-assigned image_date so a later restart can tell this
                    # exact upload apart from a reused content_id.
                    self.standby_image_date = await self._get_standby_image_date(content_id)
                    self._cache_standby_content_id(content_id, self.standby_image_date)
                    await self.tv.select_image(content_id)
                    # Publish standby via MQTT if enabled
                    if self.mqtt_enabled:
                        self._publish_mqtt_discovery()
                        self._publish_mqtt_state('Standby', 'standby.png', os.path.basename(os.path.dirname(standby_path)) or None)
                    self.log.info('Selected standby: %s (%s)', self.standby, content_id)
        except Exception as e:
            self._warn_upload_compat(e)
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
        # Restore last slideshow paths so the next seed avoids repeating the same images.
        # Fall back to deriving them from uploaded_files (backward compat with old cache).
        persisted_paths = self.cache.get('_last_slideshow_paths')
        if persisted_paths is not None:
            self._last_slideshow_paths = set(persisted_paths)
        elif self.uploaded_files:
            self._last_slideshow_paths = {
                v.get('path_rel') for v in self.uploaded_files.values() if v.get('path_rel')
            }

    def _cache_selected_collections(self):
        try:
            self.load_cache()
            self.cache['_selected_collections'] = list(self.selected_collections)
            self.save_cache()
        except Exception as e:
            self.log.warning('Failed to cache selected_collections: %s', e)

    def _load_standby_content_id(self):
        try:
            self.load_cache()
            return self.cache.get('_standby_content_id'), self.cache.get('_standby_image_date')
        except Exception:
            return None, None

    def _cache_standby_content_id(self, content_id, image_date=None):
        try:
            self.load_cache()
            self.cache['_standby_content_id'] = content_id
            self.cache['_standby_image_date'] = image_date
            self.save_cache()
        except Exception as e:
            self.log.warning('Failed to cache standby content_id: %s', e)

    def close(self):
        '''
        exit on signal
        '''
        self.log.info('SIGINT/SIGTERM received, exiting')
        os._exit(1)
        
    # Art API versions known to require the WS-binary upload transport instead of the
    # D2D socket path (2018/2019 Frame TVs and legacy Art API builds such as 1.07 on
    # the UE55LS003 / 17_KANTM_UHD).
    _WS_BINARY_API_VERSIONS = ('0.97', '1.07')

    async def get_api_version(self):
        '''
        checks api version to see if it's old (<2021) or new type
        sets api_version to 0 for old, and 1 for new

        Idempotent: this is queried early (before the first standby upload) and again
        from initialize(); once the version is known we skip the redundant TV request.
        A previous failure leaves api_version_str unset so a later call can retry.
        '''
        if self.api_version_str is not None:
            return
        try:
            api_version = await self.tv.get_api_version()
            self.log.info('API version: {}'.format(api_version))
            self.api_version_str = api_version
            self.api_version = 0 if int(api_version.replace('.','')) < 4000 else 1
        except Exception as e:
            self.log.warning('Failed to get API version: %s', e)
            self.api_version = 0
            self.api_version_failed = True
        
    async def _upload_ws_binary(self, file_data, file_type, matte):
        '''Upload image via WebSocket binary frame — required for Art API 0.97 (2018/2019 Frame TVs).

        SmartThings protocol confirmed by Wireshark (see https://github.com/xchwarze/samsung-tv-ws-api/issues/130):
          payload = uint16_be(len(header_json)) + header_json_bytes + image_bytes
        where header_json wraps the send_image request in a ms.channel.emit binary frame.
        The TV responds with an image_added d2d_service_message containing content_id.
        '''
        upload_id = str(uuid.uuid4())
        ft = file_type.lower()
        ft_hdr = 'JPEG' if ft in ('jpg', 'jpeg') else ft.upper()

        inner = json.dumps({
            'request': 'send_image',
            'file_type': ft_hdr,
            'matte_id': matte or 'none',
            'id': upload_id,
        }, separators=(',', ':'))
        outer = json.dumps({
            'method': 'ms.channel.emit',
            'params': {
                'data': inner,
                'to': 'host',
                'event': 'art_app_request',
            },
        }, separators=(',', ':')).encode('utf-8')
        payload = len(outer).to_bytes(2, 'big') + outer + file_data

        # Register response future BEFORE sending to avoid a race with fast TVs
        self.tv.pending_requests[upload_id] = asyncio.Future()
        await self.tv.start_listening()
        assert self.tv.connection, 'TV WebSocket connection not available'
        await self.tv.connection.send(payload)   # bytes → WebSocket binary frame
        data = await self.tv.wait_for_response(upload_id, timeout=30)
        return data.get('content_id') if data else None

    async def _upload_to_tv(self, file_data, file_type, matte):
        '''Route upload to the correct method based on Art API version.

        Known-legacy Art API versions (_WS_BINARY_API_VERSIONS, e.g. 0.97/1.07) use the
        WS-binary frame upload directly. Every other case tries the D2D socket upload
        first; if the TV rejects it with error -1 — the symptom of a model that silently
        requires WS-binary — we fall back to the binary path and latch it for the rest of
        the session so subsequent uploads skip the failing D2D attempt. This covers both
        TVs whose version could not be determined and TVs that report a version but still
        need WS-binary (e.g. Art API 1.07 on UE55LS003 / 17_KANTM_UHD).
        '''
        if self._ws_binary_latched or self.api_version_str in self._WS_BINARY_API_VERSIONS:
            return await self._upload_ws_binary(file_data, file_type, matte)
        try:
            return await self.tv.upload(file_data, file_type=file_type, matte=matte, portrait_matte=matte)
        except Exception as e:
            if 'error number -1' not in str(e):
                raise
            self.log.warning(
                'D2D upload failed with error -1 — retrying with WS-binary transport '
                '(legacy Frame TV fallback). api_version=%s',
                self.api_version_str or 'unknown',
            )
            result = await self._upload_ws_binary(file_data, file_type, matte)
            if result:
                # Latch so all subsequent uploads use the binary path directly.
                self._ws_binary_latched = True
                self.log.info('WS-binary upload succeeded — latching binary transport for this session')
            return result

    def _warn_upload_compat(self, error):
        '''Emit a one-time diagnostic when uploads fail with error -1.
        _upload_to_tv already retries such failures over WS-binary, so this is a
        best-effort hint for the case where the binary fallback also failed.'''
        if self._upload_compat_warned:
            return
        if 'error number -1' not in str(error):
            return
        self._upload_compat_warned = True
        self.log.warning(
            'Upload failed with error -1 and the WS-binary fallback did not recover. '
            'If this is a legacy Frame TV (2018/2019 or Art API 0.97/1.07), check the '
            'api_version reported in the logs. '
            'See https://github.com/xchwarze/samsung-tv-ws-api/issues/130 for background.'
        )

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
        # Publish current state at startup to avoid stale retained values.
        # Skip when standby was just selected — ensure_standby_selected already published
        # the correct 'Standby' state.  Polling the TV here can return the old artwork
        # content_id (display-switch lag) and would overwrite the correct retained message.
        if not self.standby_content_id:
            # If art mode hasn't been confirmed True yet, do one more check before
            # publishing.  The Samsung TV WebSocket can return an empty response to
            # the very first in_artmode() query right after a fresh connection
            # (seen most often on container restart), which makes cleanup_old_uploads()
            # set _in_art_mode=False and return early.  A single retry here, after a
            # brief pause for the TV WS to stabilise, usually gets the correct state
            # and prevents a spurious in_art_mode:false from being written to the
            # retained MQTT topic — which is what causes the "not in art mode" flash
            # on the web UI / HA card after container reboots.
            if self._in_art_mode is not True and self.tv is not None:
                try:
                    await asyncio.sleep(1)
                    await self.safe_in_artmode()
                except Exception:
                    pass
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
            if self.api_version_str in self._WS_BINARY_API_VERSIONS:
                # Legacy Frame TVs (Art API 0.97/1.07) return thumbnails as a single
                # unframed binary WebSocket packet on the main connection. The library's
                # listen loop tries to JSON-decode it, throws, and leaves the async
                # request/response dispatcher desynchronized — after which every request
                # (get_artmode, etc.) times out and rotation stalls. Skip thumbnail sync
                # entirely on these models; content IDs are tracked via the persistent
                # cache, so the PIL reconciliation isn't needed.
                self.log.info(
                    'Skipping PIL thumbnail sync on legacy Art API %s — this firmware returns '
                    'thumbnails as an unframed binary WS packet that desyncs the async dispatcher; '
                    'content IDs are tracked via the persistent cache instead.',
                    self.api_version_str,
                )
            else:
                await self.pil.initialize() #optional
        else:
            self.log.warning('syncing disabled, not updating uploaded files list')
        
        # Display art immediately after init only if TV is already in art mode.
        # If not, the main loop will pick it up when art mode is detected naturally.
        if len(self.get_content_ids()) > 0:
            if await self.safe_in_artmode():
                self.log.info('Content available after init and TV is in art mode, displaying first artwork')
                await self.change_art()
                self.start = time.time()
                self.write_program_data()
            else:
                self.log.info('Content available after init but TV is not in art mode; waiting for art mode')
        # Initialization complete — clear startup lock and let UI know it's safe
        self._startup_in_progress = False
        self._publish_slideshow_state()
        # Force-publish current artwork state so UIs clear any stale in_art_mode=false
        # retained from a previous session.
        try:
            await self._publish_current_artwork_state(force=True)
        except Exception:
            pass
        
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

    async def get_tv_content_entries(self, category='MY-C0002'):
        '''
        Full content entries (dicts including content_id and image_date) for a category,
        or None on failure. Used to match a stored standby id + image_date and detect
        content-id reuse.
        '''
        try:
            return list(await self.tv.available(category, timeout=10))
        except AssertionError:
            self.log.warning('failed to get contents from TV')
            return None
        except Exception as e:
            self.log.warning('failed to get contents from TV: %s', e)
            return None

    async def _get_standby_image_date(self, content_id):
        '''Return the TV-assigned image_date for content_id in My Photos, or None.'''
        entries = await self.get_tv_content_entries('MY-C0002')
        if entries:
            match = next((e for e in entries if e.get('content_id') == content_id), None)
            if match:
                return match.get('image_date')
        return None

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
        # Persist last slideshow paths so the next seed avoids repeating the same images
        self.cache['_last_slideshow_paths'] = list(self._last_slideshow_paths)
        self.save_cache()
            
    def read_file(self, filename):
        '''
        read image file, return file binary data and file type
        Resizes images larger than 4K to 4K to ensure compatibility with Samsung Frame TV.
        Also compresses images that exceed the Samsung TV art upload limit (~2 MB).
        '''
        # Art API 0.97 (2018/2019 Frame TVs) use a WS-binary upload path handled by _upload_ws_binary().
        # For all other TVs the standard D2D socket upload from the samsungtvws library is used.
        # File-size-based recompression via SAMSUNG_TV_ART_MAX_FILE_BYTES is unrelated to the
        # protocol version and can be used independently on any TV model.
        # Unset by default — no size-based recompression on modern TVs.
        _max_bytes_env = os.environ.get('SAMSUNG_TV_ART_MAX_FILE_BYTES', '').strip()
        MAX_UPLOAD_BYTES = int(_max_bytes_env) if _max_bytes_env else None
        # Optionally cap image dimensions, e.g. SAMSUNG_TV_ART_MAX_DIMENSION=1920x1080.
        # Useful for 2019 1080p Frame TVs where large images are rejected even under the byte limit.
        # Accepts "WxH" (e.g. "1920x1080") or a single number for square cap (e.g. "1920").
        _max_dim_env = os.environ.get('SAMSUNG_TV_ART_MAX_DIMENSION', '').strip()
        if _max_dim_env:
            _parts = _max_dim_env.lower().replace('x', ' ').split()
            MAX_DIM_W = int(_parts[0]) if _parts else None
            MAX_DIM_H = int(_parts[1]) if len(_parts) > 1 else MAX_DIM_W
        else:
            MAX_DIM_W, MAX_DIM_H = None, None
        try:
            with open(filename, 'rb') as f:
                file_data = f.read()
            file_type = self.get_file_type(filename)
            
            if HAVE_PIL and file_data:
                try:
                    img = Image.open(io.BytesIO(file_data))
                    # Ensure we have a mutable copy for any reprocessing below
                    img_fmt = img.format or 'JPEG'
                    needs_save = False

                    # Step 1a: Resize to fit 4K if dimensions exceed it
                    if img.width > 3840 or img.height > 2160:
                        self.log.info('Resizing image {} from {}x{} to fit 4K'.format(filename, img.width, img.height))
                        img.thumbnail((3840, 2160), Image.Resampling.LANCZOS)
                        needs_save = True

                    # Step 1b: Optionally cap to a user-specified max dimension
                    if MAX_DIM_W and (img.width > MAX_DIM_W or img.height > MAX_DIM_H):
                        self.log.info('Resizing image {} from {}x{} to fit {}x{}'.format(
                            filename, img.width, img.height, MAX_DIM_W, MAX_DIM_H))
                        img.thumbnail((MAX_DIM_W, MAX_DIM_H), Image.Resampling.LANCZOS)
                        needs_save = True

                    # Step 2: Re-encode to get accurate byte count after any resize,
                    # then progressively reduce JPEG quality until under the size limit.
                    if needs_save or (MAX_UPLOAD_BYTES and len(file_data) > MAX_UPLOAD_BYTES):
                        # Convert palette/RGBA to RGB for JPEG compatibility
                        if img.mode not in ('RGB', 'L'):
                            img = img.convert('RGB')
                        output = io.BytesIO()
                        quality = 92
                        img.save(output, format='JPEG', quality=quality)
                        file_data = output.getvalue()
                        file_type = 'jpg'

                        # Reduce quality in steps until the file fits
                        while MAX_UPLOAD_BYTES and len(file_data) > MAX_UPLOAD_BYTES and quality > 50:
                            quality -= 8
                            output = io.BytesIO()
                            img.save(output, format='JPEG', quality=max(quality, 50))
                            file_data = output.getvalue()

                        # If quality reduction alone isn't enough, also halve the resolution
                        # and retry. This handles 4K images on TVs with low byte limits.
                        if MAX_UPLOAD_BYTES and len(file_data) > MAX_UPLOAD_BYTES:
                            new_w, new_h = max(1, img.width // 2), max(1, img.height // 2)
                            self.log.info(
                                'Quality reduction insufficient for %s (%d bytes); '
                                'scaling to %dx%d and retrying',
                                filename, len(file_data), new_w, new_h
                            )
                            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                            quality = 92
                            output = io.BytesIO()
                            img.save(output, format='JPEG', quality=quality)
                            file_data = output.getvalue()
                            while MAX_UPLOAD_BYTES and len(file_data) > MAX_UPLOAD_BYTES and quality > 50:
                                quality -= 8
                                output = io.BytesIO()
                                img.save(output, format='JPEG', quality=max(quality, 50))
                                file_data = output.getvalue()

                        actual_quality = max(quality, 50)
                        if MAX_UPLOAD_BYTES and len(file_data) > MAX_UPLOAD_BYTES:
                            self.log.warning(
                                'Image %s is still %d bytes (quality=%d, %dx%d) after compression; '
                                'TV may reject it (limit ~%d bytes)',
                                filename, len(file_data), actual_quality,
                                img.width, img.height, MAX_UPLOAD_BYTES
                            )
                        elif needs_save or actual_quality < 92:
                            self.log.info(
                                'Image %s reprocessed to %d bytes (quality=%d, %dx%d)',
                                filename, len(file_data), actual_quality,
                                img.width, img.height
                            )
                except Exception as e:
                    self.log.warning('Failed to process image {}: {}'.format(filename, e))
            
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
        
    async def upload_files(self, filenames, progress_cb=None):
        '''
        upload files in list to tv with rate limiting to avoid overwhelming TV
        Supports both simple filenames (from current folder) and relative paths (from multi-collection mode).
        progress_cb(idx, total, display_name) is called before each upload if provided.
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
                path_rel_for_matte = filename
            else:
                # Single folder mode: simple filename
                path = os.path.join(self.folder, filename)
                display_name = filename
                # Build a path_rel under media_root if possible, for matte lookup
                try:
                    if self.media_root and os.path.commonpath([self.media_root, path]) == self.media_root:
                        path_rel_for_matte = os.path.relpath(path, self.media_root)
                    else:
                        path_rel_for_matte = filename
                except Exception:
                    path_rel_for_matte = filename
            
            # Verify file exists before attempting upload
            if not os.path.isfile(path):
                self.log.error('File not found: %s', path)
                continue
                
            file_data, file_type = self.read_file(path)
            if file_data and self.tv.art_mode:
                self.log.info('uploading : {} to tv ({}/{})'.format(display_name, idx + 1, len(filenames)))
                if progress_cb:
                    try:
                        progress_cb(idx + 1, len(filenames), display_name)
                    except Exception:
                        pass
                content_id = None
                matte_for_upload = self._resolve_matte_for(path_rel_for_matte, os.path.basename(filename))
                # NOTE: On this firmware, the matte is baked into the rendered
                # composite at upload time via the send_image matte_id field.
                # Subsequent change_matte calls update stored metadata but do
                # NOT trigger a re-render, so the only way to change an
                # image's displayed matte is to delete + re-upload it. That's
                # why we pass matte_for_upload here (not 'none').
                try:
                    content_id = await self._upload_to_tv(file_data, file_type, matte_for_upload)
                    consecutive_failures = 0  # Reset on success
                except AssertionError:
                    self.log.warning('file: %s failed to upload (empty response)', display_name)
                    consecutive_failures += 1
                except Exception as e:
                    # If the failure was the TV rejecting an invalid matte
                    # for this image (error -7), retry once with matte=none
                    # and pin the per-image override to 'none' so we don't
                    # repeat the same bad combo on future uploads.
                    if (matte_for_upload and matte_for_upload != 'none'
                            and self._is_matte_minus_7(e)):
                        self.log.warning(
                            'file: %s upload rejected by TV (-7) with matte %r; '
                            'retrying with matte=none and pinning override',
                            display_name, matte_for_upload,
                        )
                        try:
                            content_id = await self._upload_to_tv(file_data, file_type, 'none')
                        except Exception as e2:
                            self.log.warning('file: %s retry without matte also failed: %s', display_name, e2)
                        if content_id:
                            self._matte_overrides[path_rel_for_matte] = 'none'
                            self._save_matte_overrides()
                            self._publish_matte_overrides()
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                    else:
                        self._warn_upload_compat(e)
                        self.log.warning('file: %s failed to upload: %s', display_name, e)
                        consecutive_failures += 1
                # Some TV firmwares don't raise on a bad matte — they accept
                # the upload call but return no content_id. Detect that here
                # and apply the same retry-without-matte + pin-to-none path.
                if (not content_id and matte_for_upload
                        and matte_for_upload != 'none'):
                    self.log.warning(
                        'file: %s upload returned no content_id with matte %r; '
                        'retrying with matte=none and pinning override',
                        display_name, matte_for_upload,
                    )
                    try:
                        content_id = await self._upload_to_tv(file_data, file_type, 'none')
                    except Exception as e3:
                        self.log.warning('file: %s retry without matte also failed: %s', display_name, e3)
                    if content_id:
                        self._matte_overrides[path_rel_for_matte] = 'none'
                        self._save_matte_overrides()
                        self._publish_matte_overrides()
                        consecutive_failures = 0
                
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
            return True
        return False
            
    async def add_files(self, files):
        '''
        if new files found, upload to tv
        Limits uploads to avoid overwhelming the TV - we only need a few images for rotation
        When one or more collections are selected, uses collection-based randomization.
        '''
        max_uploads = int(os.environ.get('SAMSUNG_TV_ART_MAX_UPLOADS', '10'))

        # Account for images already on the TV (e.g. from failed/partial cleanup).
        # We want the total on-TV count (including what's already there) to stay at
        # or below max_uploads, not just blindly upload max_uploads more on top.
        # Standby is always present and is not a slideshow slot, so exclude it from the count.
        standby_basename = os.path.basename(self.standby) if self.standby else None
        already_on_tv = len([
            k for k in self.uploaded_files
            if k not in self.exclude and k != standby_basename
        ])
        headroom = max(0, max_uploads - already_on_tv)
        if headroom == 0:
            self.log.info('TV already has %d/%d uploads; skipping add_files', already_on_tv, max_uploads)
            return 0

        # If collections are selected, always source candidates from those collections
        # (works for single and multi-collection modes).
        collections = getattr(self, 'selected_collections', [])

        # When slideshow override is active, only upload the override files (ensure they stay on TV)
        if self.slideshow_override is not None:
            uploaded_paths = {v.get('path_rel') for v in self.uploaded_files.values()}
            missing = [p for p in self.slideshow_override if p not in uploaded_paths]
            if not missing:
                return 0  # All override files already on TV; nothing to upload
            new_files = missing[:headroom]
        elif len(collections) > 0:
            new_files = await self.get_files_from_multiple_collections(collections, headroom)
        else:
            # Fallback: legacy single-folder mode when no collections are selected
            new_files = [f for f in files if f not in self.uploaded_files.keys()]
            if len(new_files) > headroom:
                self.log.info('Limiting upload from %d to %d files to protect TV', len(new_files), headroom)
                # Always pick a random sample for variety when changing collections
                new_files = random.sample(new_files, headroom)
        
        #upload new files
        if new_files:
            # Sort for sequential playback if enabled
            if self.sequential:
                new_files = sorted(new_files)
            self.log.info('adding files to tv : {}'.format(new_files))
            await self.wait_for_files(new_files)
            await self.upload_files(new_files, progress_cb=getattr(self, '_reseed_progress_cb', None))
            return len(new_files)
        return 0

    async def get_files_from_multiple_collections(self, collections, max_uploads):
        '''
        Get files evenly distributed from multiple collections.
        If max_uploads=8 and collections=2, gets 4 from each.
        Prefers files not seen in the previous slideshow; only supplements with
        previously-shown images when the fresh pool is too small to fill all slots.
        Returns list of paths relative to media_root.
        '''
        last_paths = getattr(self, '_last_slideshow_paths', set())
        # Shuffle so remainder-based extra slots are distributed randomly each run,
        # not always biased toward the alphabetically-first collections.
        collections = list(collections)
        random.shuffle(collections)
        num_collections = len(collections)
        per_collection = max_uploads // num_collections
        remainder = max_uploads % num_collections

        self.log.info('Distributing %d uploads across %d collections (%d each, %d extra)',
                      max_uploads, num_collections, per_collection, remainder)

        # Collect fresh (not in last slideshow) and stale (were in last slideshow) separately
        fresh_by_col = []  # list of (take_count, [fresh_rel_paths])
        all_stale = []     # stale rel_paths across all collections — fallback pool

        for idx, collection in enumerate(collections):
            collection_path = os.path.join(self.media_root, collection)
            if not os.path.isdir(collection_path):
                self.log.warning('Collection directory not found: %s', collection_path)
                continue

            try:
                col_rel = [
                    os.path.join(collection, f)
                    for f in os.listdir(collection_path)
                    if os.path.isfile(os.path.join(collection_path, f))
                    and self.get_file_type(os.path.join(collection_path, f)) in self.allowed_ext
                ]
            except Exception as e:
                self.log.warning('Failed to list collection %s: %s', collection, e)
                continue

            take_count = per_collection + (1 if idx < remainder else 0)

            fresh = [f for f in col_rel if f not in last_paths]
            stale = [f for f in col_rel if f in last_paths]
            random.shuffle(fresh)
            random.shuffle(stale)

            fresh_by_col.append((take_count, fresh))
            all_stale.extend(stale)
            self.log.info('Collection %s: %d fresh, %d stale (of %d total)',
                          collection, len(fresh), len(stale), len(col_rel))

        # Fill slots from fresh files first, respecting per-collection quotas
        selected = []
        for take_count, fresh in fresh_by_col:
            selected.extend(fresh[:take_count])

        # Supplement with stale files if the fresh pool didn't fill all slots
        deficit = max_uploads - len(selected)
        if deficit > 0 and all_stale:
            random.shuffle(all_stale)
            selected.extend(all_stale[:deficit])
            self.log.info('Supplementing with %d previously-shown file(s) (fresh pool was short)',
                          min(deficit, len(all_stale)))

        # Safety filter: never re-upload something already on the TV in this session
        already_uploaded = set(self.uploaded_files.keys())
        selected = [f for f in selected if os.path.basename(f) not in already_uploaded]

        self.log.info('Total files selected for upload: %d', len(selected))
        return selected

    _PREVIEW_IMAGE_EXT = frozenset(('.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'))

    def _preview_random_selection(self, collections, max_n):
        """Return a randomly-picked list of up to max_n path_rel values from
        the given collections, using the same fresh-first logic as
        get_files_from_multiple_collections but without filtering out files
        that are already on the TV (this is a dry-run preview).

        Uses a fast extension check instead of PIL get_file_type() so that
        this method is safe to call from the synchronous paho MQTT callback
        thread — PIL disk I/O would block paho's network loop and delay the
        ack publish by as long as it takes to read every file header.
        """
        last_paths = getattr(self, '_last_slideshow_paths', set())
        collections = list(collections)
        num_collections = len(collections)
        if num_collections == 0:
            return []

        # Build one candidate per collection (prefer fresh images within each collection),
        # then shuffle the per-collection candidates so any max_n of the num_collections
        # collections can contribute equally regardless of collection size or disk order.
        # Overflow images (beyond the first per collection) go into spill pools used to
        # fill any remaining deficit after the per-collection pass.
        candidates_fresh = []  # best (fresh) candidate from each collection
        candidates_stale = []  # best (stale) candidate from collections with no fresh images
        spill_fresh = []       # additional fresh images beyond the first per collection
        spill_stale = []       # all stale images
        total_found = 0

        for collection in collections:
            collection_path = os.path.join(self.media_root, collection)
            if not os.path.isdir(collection_path):
                continue
            try:
                # Use cached listing from _publish_slideshow_available if available
                # so the first shuffle click doesn't block paho with a cold disk scan.
                raw_files = getattr(self, '_collection_file_cache', {}).get(collection_path)
                if raw_files is None:
                    raw_files = os.listdir(collection_path)
                col_rel = [
                    os.path.join(collection, f)
                    for f in raw_files
                    if f.lower() != 'standby.png'
                    and os.path.isfile(os.path.join(collection_path, f))
                    and os.path.splitext(f)[1].lower() in self._PREVIEW_IMAGE_EXT
                ]
            except Exception:
                continue

            total_found += len(col_rel)
            fresh = [f for f in col_rel if f not in last_paths]
            stale = [f for f in col_rel if f in last_paths]
            random.shuffle(fresh)
            random.shuffle(stale)

            if fresh:
                candidates_fresh.append(fresh[0])
                spill_fresh.extend(fresh[1:])
                spill_stale.extend(stale)
            elif stale:
                candidates_stale.append(stale[0])
                spill_stale.extend(stale[1:])

        # Shuffle so the cut of max_n collections is uniformly random
        random.shuffle(candidates_fresh)
        random.shuffle(candidates_stale)

        # Fresh-first: prefer fresh candidates, supplement with stale candidates, then spill
        selected = (candidates_fresh + candidates_stale)[:max_n]
        deficit = max_n - len(selected)
        if deficit > 0 and spill_fresh:
            random.shuffle(spill_fresh)
            selected.extend(spill_fresh[:deficit])
            deficit = max_n - len(selected)
        if deficit > 0 and spill_stale:
            random.shuffle(spill_stale)
            selected.extend(spill_stale[:deficit])

        self.log.info('Preview result: %d/%d images (total_available=%d across %d collections)',
                      len(selected), max_n, total_found, num_collections)
        return selected

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
        When a slideshow override is active, only returns content_ids for the override paths.
        '''
        if self.slideshow_override is not None:
            # Override mode: return content_ids in override order for paths currently on the TV
            result = []
            for path in self.slideshow_override:
                base = os.path.basename(path)
                for rec in self.uploaded_files.values():
                    pr = rec.get('path_rel', '')
                    if pr == path or pr == base or os.path.basename(pr) == base:
                        cid = rec.get('content_id')
                        if cid and cid not in result:
                            result.append(cid)
                            break
            return result
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
                if len(parts) > 1:
                    # Join all parts except the filename to support subdir collections
                    # e.g. "Artists/Kelly_Burns/file.jpg" -> collection = "Artists/Kelly_Burns"
                    collection = os.path.join(*parts[:-1])
                elif parts:
                    collection = parts[0]
            else:
                collection = os.path.basename(os.path.dirname(full_path))
        except Exception:
            collection = None
        display_name = os.path.splitext(base_name)[0]
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
            self._mqtt.on_disconnect = self._on_mqtt_disconnect_compat
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
            try:
                self._mqtt.subscribe(self.mqtt_slideshow_presets_topic, qos=1)
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

    def _generate_default_presets(self):
        """Build default saved-selection presets from installed collections."""
        csv_data = getattr(self, '_csv_by_path', {})
        if not csv_data:
            # Fall back to scanning media_root directories
            csv_data = {}
            try:
                for d in os.listdir(self.media_root):
                    dp = os.path.join(self.media_root, d)
                    if not os.path.isdir(dp):
                        continue
                    for f in os.listdir(dp):
                        if f.lower().endswith(('.jpg','.jpeg','.png')) and f != 'standby.png':
                            csv_data[f'{d}/{f}'] = {'artwork_title': f, 'artwork_dir': d}
            except Exception:
                pass

        def _m(path, title, *kw):
            t = (path + ' ' + title).lower()
            return any(re.search(r'\b' + k + r'\b', t) for k in kw)

        marine_kw = ['sea','ocean','coast','coastal','beach','wave','waves','marine',
                     'harbour','harbor','port','sail','sailing','sailboat','boat','boats',
                     'ship','ships','fishing','steamer','pier','dock','bay','fleet',
                     'canal','barge','schooner','yacht','regatta','rowboat','whaling',
                     'seascape','fisherman','fishermen']
        land_kw   = ['landscape','valley','mountain','mountains','hill','hills','forest',
                     'woodland','meadow','field','countryside','rural','farm','pastoral',
                     'garden','park','river','lake','pond','waterfall','cliff','canyon',
                     'desert','moor','fjord','wilderness','trees']
        marine_excl = ['sea','ocean','harbour','harbor','port','sail','boat','ship',
                       'vessel','pier','dock','fleet','marine','coastal','beach','schooner']

        landscape_artists = {'Caspar_David_Friedrich','Albert_Bierstadt','Adalbert_Stifter',
                              'Antoine_Chintreuil','Arthur_Streeton','Frederick_McCubbin'}
        marine_artists    = {'Eugene_Boudin','Jacob_Maris','George_Wesley_Bellows'}
        imp_artists       = {'Claude_Monet','Pierre-Auguste_Renoir','Alfred_Sisley',
                              'Camille_Pissarro','Berthe_Morisot','Mary_Cassatt','Childe_Hassam'}
        abstract_artists  = {'Jackson_Pollock','Mark_Rothko','Paul_Klee','Max_Ernst',
                              'Franz_Marc','Andy_Warhol','Keith_Haring','Banksy',
                              'Pablo_Picasso','Marc_Chagall','Henri_Matisse','Gerhard Richter'}
        west_artists      = {'Frederic_Remington','Charles_Marion_Russell'}

        def artist_of(path):
            return path.split('/')[0] if '/' in path else ''

        landscapes, marine, impressionism, abstract_mod, west, portraits = [], [], [], [], [], []
        for path, row in csv_data.items():
            title = (row.get('artwork_title') or '').strip()
            art = artist_of(path)
            is_marine = _m(path, title, *marine_kw)
            is_land   = _m(path, title, *land_kw) and not _m(path, title, *marine_excl)
            if (art in landscape_artists and not is_marine) or \
               (is_land and art not in abstract_artists and art not in {'Banksy','Andy_Warhol',
                'Keith_Haring','Jackson_Pollock','Mark_Rothko','Pablo_Picasso','Henri_Matisse',
                'Marc_Chagall','Paul_Klee','Max_Ernst','Alphonse_Mucha','El_Greco',
                'Diego_Velazquez','Sandro_Botticelli','Francois_Boucher','Gustav_Klimt',
                'Rembrandt_Harmenszoon_van_Rijn','Leonardo_da_Vinci'}):
                landscapes.append(path)
            if art in marine_artists or is_marine:
                marine.append(path)
            if art in imp_artists:
                impressionism.append(path)
            if art in abstract_artists:
                abstract_mod.append(path)
            if art in west_artists:
                west.append(path)
            if _m(path, title, 'portrait', 'self-portrait', 'bust') and \
               not _m(path, title, 'landscape','mountain','forest','field','river','lake','boat','ship'):
                portraits.append(path)

        CHUNK = 30
        presets = []
        for name, paths in [
            ('Landscapes',       landscapes),
            ('Boats & Marine',    marine),
            ('Impressionism',     impressionism),
            ('Abstract & Modern', abstract_mod),
            ('American West',     west),
            ('Portraits',         portraits),
        ]:
            unique = sorted(set(paths))
            if not unique:
                continue
            if len(unique) <= CHUNK:
                presets.append({'name': name, 'paths': unique})
            else:
                # Split into numbered sub-presets of CHUNK images each
                chunks = [unique[i:i+CHUNK] for i in range(0, len(unique), CHUNK)]
                for idx, chunk in enumerate(chunks, 1):
                    presets.append({'name': f'{name} {idx}', 'paths': chunk})
        return presets

    def _bootstrap_default_presets(self):
        """Publish default presets if no retained message arrived after connect."""
        if self._presets_from_broker:
            return
        if not self._mqtt or not self._mqtt_is_connected:
            return
        try:
            defaults = self._generate_default_presets()
            if defaults:
                self._save_slideshow_presets(defaults)
                self._mqtt.publish(
                    self.mqtt_slideshow_presets_topic,
                    json.dumps(defaults),
                    qos=1, retain=True,
                )
                self.log.info('Published %d default preset(s) to %s',
                              len(defaults), self.mqtt_slideshow_presets_topic)
        except Exception as e:
            self.log.warning('Failed to publish default presets: %s', e)

    def _on_mqtt_message(self, client, userdata, msg):
        try:
            topic = getattr(msg, 'topic', '')
            if topic == self.mqtt_slideshow_presets_topic:
                self._presets_from_broker = True
                if self._presets_bootstrap_timer:
                    self._presets_bootstrap_timer.cancel()
                    self._presets_bootstrap_timer = None
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
                cmd = topic[len(self.mqtt_cmd_prefix)+1:]
                log_fn = self.log.debug if cmd == 'slideshow/available/request' else self.log.info
                log_fn('Received MQTT command on %s: %s', topic, payload_raw if isinstance(payload_raw, str) else '<binary>')
                self._handle_mqtt_command(cmd, payload_raw)
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
                # Attach MQTT log handler so all INFO+ records stream to frame_tv/log
                if not getattr(self, '_mqtt_log_handler', None):
                    self._mqtt_log_handler = MQTTLogHandler(client)
                    logging.getLogger().addHandler(self._mqtt_log_handler)
                # Ensure subscriptions are in place after reconnects
                try:
                    if self.selection_from_mqtt:
                        client.subscribe(self.selection_mqtt_topic, qos=1)
                    client.subscribe(f"{self.mqtt_cmd_prefix}/#", qos=1)
                    client.subscribe(self.mqtt_slideshow_presets_topic, qos=1)
                except Exception:
                    pass
                # Bootstrap presets:
                #  1) If we have presets persisted on disk, republish them as retained
                #     so the broker (which may have lost retain across its own restart
                #     or a fresh container) is reseeded from our local source of truth.
                #  2) Otherwise, wait briefly for a retained message from the broker;
                #     if none arrives, generate defaults from installed collections.
                if self._slideshow_presets:
                    try:
                        client.publish(
                            self.mqtt_slideshow_presets_topic,
                            json.dumps(self._slideshow_presets),
                            qos=1, retain=True,
                        )
                        self._presets_from_broker = True
                        self.log.info('Republished %d persisted preset(s) to %s',
                                      len(self._slideshow_presets), self.mqtt_slideshow_presets_topic)
                    except Exception as e:
                        self.log.warning('Failed to republish persisted presets: %s', e)
                elif not self._presets_from_broker:
                    if self._presets_bootstrap_timer:
                        self._presets_bootstrap_timer.cancel()
                    t = threading.Timer(3.0, self._bootstrap_default_presets)
                    t.daemon = True
                    t.start()
                    self._presets_bootstrap_timer = t
                # Republish retained state so broker always has fresh data after reconnect
                try:
                    self._publish_collections_state()
                    self._publish_settings_state()
                    self._publish_matte_overrides()
                except Exception:
                    pass
                # Reset artwork state refresh timer so the main loop immediately
                # re-publishes the current artwork state after any (re)connect.
                self._last_state_publish = 0
            else:
                self.log.warning('MQTT connect failed (rc=%s)', str(rc_val))
        except Exception:
            pass

    def _on_mqtt_disconnect(self, client, userdata, rc, properties=None):
        try:
            # Guard against paho firing the callback twice for the same disconnection
            if not self._mqtt_is_connected:
                return
            self._mqtt_is_connected = False
            rc_val = getattr(rc, 'value', rc)
            self.log.warning('MQTT disconnected (rc=%s)', str(rc_val))
            # Detach MQTT log handler on disconnect to avoid publish errors
            if getattr(self, '_mqtt_log_handler', None):
                logging.getLogger().removeHandler(self._mqtt_log_handler)
                self._mqtt_log_handler = None
        except Exception:
            pass

    # paho v2 passes extra positional args (disconnect_flags, reason_code, properties).
    def _on_mqtt_disconnect_compat(self, client, userdata, *args, **kwargs):
        try:
            # Last positional arg before any trailing properties is the rc/reason_code
            rc = args[0] if args else 0
            return self._on_mqtt_disconnect(client, userdata, rc)
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
        if not self.mqtt_discovery:
            # Discovery disabled — just mark availability and skip HA config payload
            try:
                self._publish_and_wait(f"{self.mqtt_state_topic}/availability", "online", qos=1, retain=True)
            except Exception:
                pass
            self._mqtt_config_published = True
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
            # Only include in_art_mode when we actually know.  Writing False
            # while the state is still None (unknown) retains a stale 'Not in
            # art mode' for the web UI until the next confirmed-True transition.
            if self._in_art_mode is not None:
                attrs["in_art_mode"] = bool(self._in_art_mode)
            # Merge CSV columns (ensure every header key exists, even if blank)
            if self._csv_headers:
                path_key = f"{collection}/{file}" if collection and file else None
                row = (path_key and getattr(self, '_csv_by_path', {}).get(path_key)) or self._csv_by_file.get(file or "") or {}
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
        """Return list of collection paths relative to media_root.
        A directory is a flat collection if it contains image files directly.
        A directory with no images but with image-containing subdirectories is
        treated as a multi-collection repo; each qualifying subdir is returned
        as 'repo/subdir' (using os.sep-compatible join).
        """
        SKIP = {'@eaDir', '@tmp'}
        IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.tif'}

        def _has_images(path):
            try:
                return any(
                    os.path.isfile(os.path.join(path, f))
                    and os.path.splitext(f)[1].lower() in IMAGE_EXT
                    for f in os.listdir(path)
                )
            except Exception:
                return False

        try:
            result = []
            for d in sorted(os.listdir(self.media_root)):
                if d in SKIP:
                    continue
                dir_path = os.path.join(self.media_root, d)
                if not os.path.isdir(dir_path):
                    continue
                if _has_images(dir_path):
                    # Flat collection — images live directly in this directory
                    result.append(d)
                else:
                    # Possibly a multi-collection repo — check one level deeper
                    try:
                        for s in sorted(os.listdir(dir_path)):
                            if s in SKIP:
                                continue
                            sub_path = os.path.join(dir_path, s)
                            if os.path.isdir(sub_path) and _has_images(sub_path):
                                result.append(os.path.join(d, s))
                    except Exception:
                        pass
            return result
        except Exception as e:
            self.log.warning('Failed to scan collections in %s: %s', self.media_root, e)
            return []

    def _publish_collections_discovery(self):
        if not self.mqtt_enabled or not self._mqtt or not self.mqtt_discovery:
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
        if not self.mqtt_enabled or not self._mqtt or not self.mqtt_discovery:
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
                "SAMSUNG_TV_ART_SEQUENTIAL": '1' if self.sequential else '0',
                "SAMSUNG_TV_ART_MQTT_HOST": str(os.environ.get('SAMSUNG_TV_ART_MQTT_HOST', self.mqtt_host or '')),
                "SAMSUNG_TV_ART_MQTT_PORT": str(os.environ.get('SAMSUNG_TV_ART_MQTT_PORT', str(self.mqtt_port or 1883))),
                "SAMSUNG_TV_ART_MQTT_WS_HOST": str(os.environ.get('SAMSUNG_TV_ART_MQTT_WS_HOST', '')),
                "SAMSUNG_TV_ART_MQTT_WS_PORT": str(os.environ.get('SAMSUNG_TV_ART_MQTT_WS_PORT', '9001')),
                "SAMSUNG_TV_ART_MQTT_USERNAME": str(os.environ.get('SAMSUNG_TV_ART_MQTT_USERNAME', self.mqtt_username or '')),
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

    # ── Slideshow override persistence ──────────────────────────────────────

    def _load_slideshow_override(self):
        """Load persisted slideshow override from /data/slideshow_override.json."""
        try:
            if os.path.isfile(self.slideshow_override_path):
                with open(self.slideshow_override_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                paths = data.get('paths', [])
                self.slideshow_override = paths if paths else None
                if self.slideshow_override:
                    self.log.info('Loaded slideshow override with %d paths', len(self.slideshow_override))
        except Exception as e:
            self.log.warning('Failed to load slideshow override: %s', e)
            self.slideshow_override = None

    def _save_slideshow_override(self):
        """Persist slideshow override to /data/slideshow_override.json."""
        try:
            os.makedirs('/data', exist_ok=True)
            data = {'paths': list(self.slideshow_override) if self.slideshow_override else []}
            with open(self.slideshow_override_path, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except Exception as e:
            self.log.warning('Failed to save slideshow override: %s', e)

    # ── Slideshow presets persistence ──────────────────────────────────────

    def _load_slideshow_presets(self):
        """Load persisted slideshow presets from /data/slideshow_presets.json."""
        try:
            if os.path.isfile(self.slideshow_presets_path):
                with open(self.slideshow_presets_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._slideshow_presets = data
                    self.log.info('Loaded %d persisted slideshow preset(s)', len(data))
        except Exception as e:
            self.log.warning('Failed to load slideshow presets: %s', e)
            self._slideshow_presets = []

    def _save_slideshow_presets(self, presets):
        """Persist slideshow presets to /data/slideshow_presets.json."""
        try:
            os.makedirs('/data', exist_ok=True)
            with open(self.slideshow_presets_path, 'w', encoding='utf-8') as f:
                json.dump(presets if isinstance(presets, list) else [], f)
            self._slideshow_presets = presets if isinstance(presets, list) else []
        except Exception as e:
            self.log.warning('Failed to save slideshow presets: %s', e)

    # ── Per-image matte overrides ───────────────────────────────────────────

    def _load_matte_overrides(self):
        """Load persisted per-image matte overrides from disk."""
        try:
            if os.path.isfile(self.matte_overrides_path):
                with open(self.matte_overrides_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # filter to {str: str}
                    self._matte_overrides = {
                        str(k): str(v) for k, v in data.items()
                        if isinstance(k, str) and isinstance(v, str) and v
                    }
                    self.log.info('Loaded %d persisted matte override(s)', len(self._matte_overrides))
        except Exception as e:
            self.log.warning('Failed to load matte overrides: %s', e)
            self._matte_overrides = {}

    def _save_matte_overrides(self):
        """Persist per-image matte overrides to disk."""
        try:
            os.makedirs('/data', exist_ok=True)
            with open(self.matte_overrides_path, 'w', encoding='utf-8') as f:
                json.dump(self._matte_overrides, f)
        except Exception as e:
            self.log.warning('Failed to save matte overrides: %s', e)

    def _resolve_matte_for(self, path_rel, fname=None):
        """Resolve effective matte for an image.
        Priority: per-image override → CSV 'matte' column → global self.matte default.
        """
        try:
            if path_rel and path_rel in self._matte_overrides:
                return self._matte_overrides[path_rel]
            if fname and fname in self._matte_overrides:
                return self._matte_overrides[fname]
            csv_rec = (
                (path_rel and getattr(self, '_csv_by_path', {}).get(path_rel))
                or (fname and self._csv_by_file.get(fname))
                or {}
            )
            csv_matte = (csv_rec.get('matte') or '').strip()
            if csv_matte:
                return csv_matte
        except Exception:
            pass
        return self.matte

    def _publish_matte_overrides(self):
        """Publish current per-image matte overrides as a retained MQTT map."""
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            payload = json.dumps(self._matte_overrides, separators=(',', ':'))
            self._mqtt.publish(self.mqtt_slideshow_mattes_topic, payload, qos=1, retain=True)
        except Exception as e:
            self.log.debug('MQTT mattes publish failed: %s', e)

    async def _ensure_matte_options_cache(self):
        """Populate self._matte_options_cache from the TV (or fallback). Safe to
        call from anywhere — no MQTT side effects. Used by both the MQTT
        publisher and the background matte probe worker."""
        if self._matte_options_cache is not None:
            return
        try:
            mattes = await self.tv.get_matte_list(True)
            if isinstance(mattes, (list, tuple)) and len(mattes) >= 2:
                types = [m.get('matte_type') for m in mattes[0] if isinstance(m, dict) and m.get('matte_type')]
                colors = [m.get('color') for m in mattes[1] if isinstance(m, dict) and m.get('color')]
                self._matte_options_cache = {'matte_types': types, 'matte_colors': colors}
            elif isinstance(mattes, dict):
                self._matte_options_cache = {
                    'matte_types': [m.get('matte_type') for m in (mattes.get('matte_types') or []) if isinstance(m, dict) and m.get('matte_type')],
                    'matte_colors': [m.get('color') for m in (mattes.get('matte_colors') or []) if isinstance(m, dict) and m.get('color')],
                }
        except Exception as e:
            self.log.debug('get_matte_list failed, using fallback: %s', e)
            self._matte_options_cache = {
                'matte_types':  ['none', 'shadowbox', 'modern', 'modernthin', 'modernwide',
                                 'flexible', 'panoramic', 'triptych', 'mix', 'squares'],
                'matte_colors': ['polar', 'neutral', 'apricot', 'sand', 'seafoam', 'lavender',
                                 'burgandy', 'navy', 'forest', 'dark', 'warm', 'sage'],
            }

    async def _publish_matte_options(self):
        """Fetch (and cache) the TV's matte_type/matte_color list, publish as retained."""
        if not self.mqtt_enabled or not self._mqtt:
            return
        await self._ensure_matte_options_cache()
        try:
            payload = json.dumps(self._matte_options_cache, separators=(',', ':'))
            self._mqtt.publish(self.mqtt_slideshow_matte_options_topic, payload, qos=1, retain=True)
        except Exception as e:
            self.log.debug('MQTT matte_options publish failed: %s', e)

    @staticmethod
    def _is_matte_minus_7(exc):
        """The Samsung Art API returns error code -7 when it rejects a matte
        combo for an image (or, harmlessly, when 'none' is reapplied to an
        image that already has no matte). We treat -7 specially in two places:
          1. `slideshow/matte/set` handler — surface a red toast and revert.
          2. `_reapply_matte_for` (per-cycle reapply) — drop the override back
             to 'none' so we don't keep retrying a combo the TV won't accept."""
        msg = str(exc)
        return 'error number -7' in msg or "'-7'" in msg or 'errno -7' in msg

    def _publish_slideshow_state(self):
        """Publish current slideshow mode, settings, and active paths to MQTT."""
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            mode = 'override' if self.slideshow_override is not None else 'auto'
            current_paths = [v.get('path_rel', k) for k, v in self.uploaded_files.items()]
            missing_rel = [k for k, v in self.uploaded_files.items() if not v.get('path_rel')]
            if missing_rel:
                self.log.warning('uploaded_files entries missing path_rel (will use basename): %s', missing_rel)
            attrs = {
                'mode': mode,
                'sequential': self.sequential,
                'update_minutes': int(max(0, (self.update_time or 0) / 60)),
                'max_uploads': self.max_uploads,
                'override_paths': list(self.slideshow_override) if self.slideshow_override else [],
                'current_paths': current_paths,
                'uploading': bool(self._refresh_in_progress or getattr(self, '_startup_in_progress', False)),
            }
            try:
                self._publish_and_wait(self.mqtt_slideshow_state_topic, mode, qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_slideshow_state_topic, mode, qos=1, retain=True)
            try:
                self._publish_and_wait(self.mqtt_slideshow_attr_topic, json.dumps(attrs, separators=(',', ':')), qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_slideshow_attr_topic, json.dumps(attrs, separators=(',', ':')), qos=0, retain=True)
        except Exception as e:
            self.log.warning('MQTT slideshow state publish failed: %s', e)

    def _publish_slideshow_available(self, override_collections=None):
        """Scan selected collections on disk and publish full image list to MQTT.
        Pass override_collections to preview a candidate set without committing it.
        """
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            # Build a set of path_rel values currently uploaded to the TV
            uploaded_paths = {v.get('path_rel', k) for k, v in self.uploaded_files.items()}
            effective_collections = override_collections if override_collections is not None else (self.selected_collections or [])
            selected_set = set(effective_collections)

            images = []
            for collection in list(effective_collections):
                coll_path = os.path.join(self.media_root, collection)
                if not os.path.isdir(coll_path):
                    continue
                try:
                    raw_files = os.listdir(coll_path)
                    # Cache the raw listing so _preview_random_selection can reuse it
                    # without a second cold scan on first shuffle click.
                    if override_collections is None:
                        if not hasattr(self, '_collection_file_cache'):
                            self._collection_file_cache = {}
                        self._collection_file_cache[coll_path] = raw_files
                    files = sorted([
                        f for f in raw_files
                        if f.lower().endswith(('.jpg', '.jpeg', '.png'))
                        and f not in ('standby.png',)
                    ])
                except Exception:
                    continue
                for fname in files:
                    path_rel = f"{collection}/{fname}"
                    csv_rec = getattr(self, '_csv_by_path', {}).get(path_rel) or self._csv_by_file.get(fname, {})
                    artist = (csv_rec.get('artist_name') or '').strip()
                    if not artist:
                        artist = getattr(self, '_dir_to_artist', {}).get(collection, '').strip()
                    images.append({
                        'path': path_rel,
                        'folder': collection,
                        'file': fname,
                        'title': (csv_rec.get('title') or csv_rec.get('artwork_title') or '').strip(),
                        'artist': artist,
                        'year': (csv_rec.get('year') or '').strip(),
                        'uploaded': path_rel in uploaded_paths,
                    })

            # Cap to 5000 images to keep the MQTT payload manageable while covering
            # large libraries (at ~200 bytes/entry, 5000 ≈ 1MB — fine for any modern broker).
            # Always include every uploaded file so the uploaded-overlap count stays correct.
            if len(images) > 5000:
                uploaded_imgs = [img for img in images if img['uploaded']]
                other_imgs = [img for img in images if not img['uploaded']]
                images = uploaded_imgs + other_imgs[:max(0, 5000 - len(uploaded_imgs))]

            payload = json.dumps({'images': images}, separators=(',', ':'))
            try:
                self._publish_and_wait(self.mqtt_slideshow_available_topic, payload, qos=1, retain=True)
            except Exception:
                self._mqtt.publish(self.mqtt_slideshow_available_topic, payload, qos=0, retain=True)
            avail_paths = {img['path'] for img in images}
            uploaded_overlap = uploaded_paths & avail_paths
            self.log.debug('Published slideshow available: %d images across %d collections (uploaded overlap: %d/%d)',
                          len(images), len(selected_set), len(uploaded_overlap), len(uploaded_paths))
        except Exception as e:
            self.log.warning('MQTT slideshow available publish failed: %s', e)

    async def _apply_slideshow_override(self, paths, req_id=None, new_collections=None, max_uploads=None):
        """Apply a specific image selection to the TV.
        If all requested images are already uploaded: simply restricts which play (lightweight).
        If any images need uploading: performs a full clean reseed with the specified paths.
        When max_uploads is provided, persists the new limit to overrides.env.
        """
        def ack(status, msg):
            self._publish_ack('slideshow/override/set', status, msg, req_id)

        needs_cleanup = False
        try:
            if new_collections is not None:
                # Commit the collection selection without triggering a separate full reseed.
                # Also sync self.folder so apply_selection() sees no pending change.
                self.selected_collections = new_collections
                self._pending_selection_change = False
                desired = self.get_selected_folder()
                if os.path.isdir(desired):
                    self.folder = desired
                self._publish_selected_collections_state()
                self._cache_selected_collections()
                self.log.info('Collections committed atomically with override: %s', new_collections)

            # Optionally update max_uploads (user manually exceeded the limit)
            if max_uploads is not None and isinstance(max_uploads, int) and max_uploads > 0:
                self.max_uploads = max_uploads
                os.environ['SAMSUNG_TV_ART_MAX_UPLOADS'] = str(max_uploads)
                self._write_overrides({'SAMSUNG_TV_ART_MAX_UPLOADS': str(max_uploads)})
                self.log.info('max_uploads updated to %d', max_uploads)

            uploaded_paths = {v.get('path_rel', k) for k, v in self.uploaded_files.items()}
            to_upload = [p for p in paths if p not in uploaded_paths]
            needs_cleanup = bool(to_upload)

            if needs_cleanup:
                # Full clean reseed with exactly the specified paths
                self._refresh_in_progress = True
                self._publish_slideshow_state()
                ack('progress', 'Preparing TV — switching to standby...')
                await self.ensure_standby_selected()
                if self.standby_content_id:
                    try:
                        await self.tv.select_image(self.standby_content_id)
                        self._publish_mqtt_state('Standby', 'standby.png', None)
                    except Exception as e:
                        self.log.warning('Failed to select standby: %s', e)
                        self.standby_content_id = None

                # Snapshot current uploads so the next shuffle prefers fresh images
                self._last_slideshow_paths = {
                    v.get('path_rel') for v in self.uploaded_files.values() if v.get('path_rel')
                }

                _existing = len([k for k in self.uploaded_files if k not in self.exclude and k != (os.path.basename(self.standby) if self.standby else None)])
                ack('progress', f'Removing {_existing} old upload(s) from TV...' if _existing else 'Removing old uploads from TV...')
                await self.cleanup_old_uploads()

                if self.standby_content_id:
                    try:
                        await self.tv.select_image(self.standby_content_id)
                    except Exception:
                        pass

                ack('progress', f'Uploading {len(paths)} image(s)...')
                await self.upload_files(paths)
                await asyncio.sleep(2)

            self.slideshow_override = paths
            self._save_slideshow_override()
            self.shown_content_ids = set()
            # Mark the applied paths as "previously seen" so the next shuffle
            # treats them as stale and prefers different images.
            self._last_slideshow_paths = set(paths)
            self._publish_slideshow_state()
            await self.change_art()
            if needs_cleanup:
                self.start = time.time()
                self.write_program_data()  # persists _last_slideshow_paths via cache
            ack('ok', f'Applied {len(paths)} image(s)')
        except Exception as e:
            self.log.warning('Error applying selection: %s', e)
            ack('error', str(e))
            raise
        finally:
            if needs_cleanup:
                self._refresh_in_progress = False
                self._publish_slideshow_state()
                self._publish_slideshow_available()
                try:
                    await self._publish_current_artwork_state(force=True)
                except Exception:
                    pass

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
                'SAMSUNG_TV_ART_SEQUENTIAL',
                'SAMSUNG_TV_ART_MQTT_HOST',
                'SAMSUNG_TV_ART_MQTT_WS_HOST',
                'SAMSUNG_TV_ART_MQTT_PORT',
                'SAMSUNG_TV_ART_MQTT_WS_PORT',
                'SAMSUNG_TV_ART_MQTT_USERNAME',
                'SAMSUNG_TV_ART_MQTT_PASSWORD',
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
                    csv_dirs = set()
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
                            csv_dirs.add(dn)
                    # Always merge in any on-disk folders not covered by the CSV so
                    # collections without a CSV entry still appear in the dropdown.
                    # Use only the basename (leaf) as the display label so subdir collections
                    # like "Artists/Kelly_Burns" show as "Kelly Burns", not the full path.
                    for d in self._scan_collections():
                        if d not in csv_dirs:
                            pairs.add(os.path.basename(d).replace('_', ' '))
                    opts = sorted(pairs)
                    if not opts:
                        # Fallback to folders if CSV produced nothing usable
                        self.log.info(
                            'No usable labels found in CSV (rows=%d, headers=%s); falling back to folder scan',
                            len(self._csv_by_file), self._csv_headers
                        )
                        opts = self._scan_collections()
                    else:
                        self.log.info('Publishing %d collections from CSV+scan', len(opts))
                        self.log.debug('Publishing collections from CSV+scan: %s', opts)
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

    def _publish_ack(self, cmd, status='ok', message='', req_id=None, extra=None):
        if not self.mqtt_enabled or not self._mqtt:
            return
        try:
            ack = {"cmd": cmd, "status": status}
            if message:
                ack["message"] = message
            if req_id is not None:
                ack["req_id"] = req_id
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if k not in ack:
                        ack[k] = v
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
                # Note: do NOT publish slideshow/available here — the reseed triggered by
                # _pending_selection_change will call _publish_slideshow_available() when
                # it completes. Publishing it now would arrive before uploading=true,
                # causing the UI to prematurely clear slideshowUserRefreshPending and
                # seed the grid from stale current_paths before the new artwork is ready.
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
                self._publish_ack('collections/refresh', 'queued', 'Collections refresh queued', req_id)
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
                    if 'SAMSUNG_TV_ART_MQTT_HOST' in data:
                        updates['SAMSUNG_TV_ART_MQTT_HOST'] = str(data['SAMSUNG_TV_ART_MQTT_HOST']).strip()
                    if 'SAMSUNG_TV_ART_MQTT_WS_HOST' in data:
                        updates['SAMSUNG_TV_ART_MQTT_WS_HOST'] = str(data['SAMSUNG_TV_ART_MQTT_WS_HOST']).strip()
                    if 'SAMSUNG_TV_ART_MQTT_PORT' in data:
                        updates['SAMSUNG_TV_ART_MQTT_PORT'] = str(int(data['SAMSUNG_TV_ART_MQTT_PORT']))
                    if 'SAMSUNG_TV_ART_MQTT_WS_PORT' in data:
                        updates['SAMSUNG_TV_ART_MQTT_WS_PORT'] = str(int(data['SAMSUNG_TV_ART_MQTT_WS_PORT']))
                    if 'SAMSUNG_TV_ART_MQTT_USERNAME' in data:
                        updates['SAMSUNG_TV_ART_MQTT_USERNAME'] = str(data['SAMSUNG_TV_ART_MQTT_USERNAME']).strip()
                    if 'SAMSUNG_TV_ART_MQTT_PASSWORD' in data:
                        updates['SAMSUNG_TV_ART_MQTT_PASSWORD'] = str(data['SAMSUNG_TV_ART_MQTT_PASSWORD'])
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
                        if 'SAMSUNG_TV_ART_MAX_UPLOADS' in updates:
                            self.max_uploads = int(updates['SAMSUNG_TV_ART_MAX_UPLOADS'])
                            self._publish_slideshow_state()
                    except Exception:
                        pass
                    self._publish_settings_state()
                    # Indicate if restart is recommended (e.g., TV IP or MQTT change)
                    msg = 'Settings updated'
                    try:
                        if 'SAMSUNG_TV_ART_TV_IP' in updates and updates['SAMSUNG_TV_ART_TV_IP'] and updates['SAMSUNG_TV_ART_TV_IP'] != str(self.ip):
                            msg += ' (TV IP change will apply after restart)'
                            self.ip = updates['SAMSUNG_TV_ART_TV_IP']
                        if any(k in updates for k in ('SAMSUNG_TV_ART_MQTT_HOST', 'SAMSUNG_TV_ART_MQTT_PORT', 'SAMSUNG_TV_ART_MQTT_USERNAME', 'SAMSUNG_TV_ART_MQTT_PASSWORD')):
                            msg += ' (MQTT changes will apply after restart)'
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
            if cmd == 'slideshow/settings/set':
                if not isinstance(data, dict):
                    self._publish_ack('slideshow/settings/set', 'error', 'Invalid JSON', req_id)
                    return
                updates = {}
                try:
                    if 'sequential' in data:
                        self.sequential = bool(data['sequential'])
                        updates['SAMSUNG_TV_ART_SEQUENTIAL'] = '1' if self.sequential else '0'
                    if 'update_minutes' in data:
                        minutes = max(0, int(float(data['update_minutes'])))
                        self.update_time = minutes * 60
                        self.start = time.time()  # Reset slideshow timer
                        updates['SAMSUNG_TV_ART_UPDATE_MINUTES'] = str(minutes)
                except Exception:
                    self._publish_ack('slideshow/settings/set', 'error', 'Validation failed', req_id)
                    return
                self._write_overrides(updates)
                self._publish_slideshow_state()
                self._publish_settings_state()
                self._publish_ack('slideshow/settings/set', 'ok', 'Slideshow settings updated', req_id)
                return
            if cmd == 'slideshow/override/set':
                paths = []
                if data and 'paths' in data and isinstance(data['paths'], list):
                    paths = [str(p).strip() for p in data['paths'] if str(p).strip()]
                if not paths:
                    self._publish_ack('slideshow/override/set', 'error', 'No paths provided', req_id)
                    return
                # Optional 'collections' field: commit collection changes atomically with override.
                new_cols = None
                if data and 'collections' in data and isinstance(data['collections'], list):
                    raw_cols = [str(c).strip() for c in data['collections'] if str(c).strip()]
                    new_cols = [self._map_to_artwork_dir(c) or c for c in raw_cols if (self._map_to_artwork_dir(c) or c)]
                # Optional 'max_uploads' field: user manually exceeded the limit.
                new_max = None
                if data and 'max_uploads' in data:
                    try:
                        new_max = int(data['max_uploads'])
                    except (ValueError, TypeError):
                        new_max = None
                fut = self._schedule_command_coro(self._apply_slideshow_override(paths, req_id, new_collections=new_cols, max_uploads=new_max), 'slideshow/override/set')
                if not fut:
                    self._publish_ack('slideshow/override/set', 'error', 'Failed to queue override apply', req_id)
                return
            if cmd == 'slideshow/presets/set':
                # Clients publish their full presets array here; backend persists it
                # to disk and re-publishes as a retained message so all other clients
                # receive it on subscribe and so it survives container recreation.
                try:
                    presets = json.loads(payload_raw) if payload_raw else []
                    if not isinstance(presets, list):
                        presets = []
                    self._save_slideshow_presets(presets)
                    self._mqtt.publish(
                        self.mqtt_slideshow_presets_topic,
                        json.dumps(presets),
                        qos=1,
                        retain=True,
                    )
                    self._publish_ack('slideshow/presets/set', 'ok', 'Presets saved', req_id)
                except Exception as e:
                    self._publish_ack('slideshow/presets/set', 'error', str(e), req_id)
                return
            if cmd == 'slideshow/presets/generate':
                # Offload to the event loop (and a thread executor for the CPU/IO
                # work) so we never block paho's network loop thread — a long
                # callback here can starve the MQTT keepalive and trigger a
                # broker disconnect ([Errno 32] Broken pipe).
                async def _do_generate_presets():
                    try:
                        loop = asyncio.get_running_loop()
                        generated = await loop.run_in_executor(None, self._generate_default_presets)
                        if not generated:
                            self._publish_ack('slideshow/presets/generate', 'error', 'No images found', req_id)
                            return
                        self._save_slideshow_presets(generated)
                        self._mqtt.publish(
                            self.mqtt_slideshow_presets_topic,
                            json.dumps(generated),
                            qos=1,
                            retain=True,
                        )
                        self._publish_ack('slideshow/presets/generate', 'ok',
                                          f'{len(generated)} preset(s) generated', req_id)
                    except Exception as e:
                        self._publish_ack('slideshow/presets/generate', 'error', str(e), req_id)
                fut = self._schedule_command_coro(_do_generate_presets(), 'slideshow/presets/generate')
                if not fut:
                    self._publish_ack('slideshow/presets/generate', 'error', 'Failed to queue preset generation', req_id)
                return
            if cmd == 'slideshow/override/clear':
                self.slideshow_override = None
                self._save_slideshow_override()
                self.shown_content_ids = set()
                self._publish_slideshow_state()
                self._publish_ack('slideshow/override/clear', 'ok', 'Slideshow override cleared; auto mode restored', req_id)
                return
            if cmd == 'slideshow/matte/set':
                # Save the per-image matte override silently. We do NOT call
                # change_matte here — on this firmware that's a no-op for the
                # rendered image (the matte is baked at upload time). The user
                # makes the change visible by clicking "Apply now" in the
                # modal, which fires slideshow/matte/apply (delete + reupload).
                try:
                    if not isinstance(data, dict):
                        self._publish_ack('slideshow/matte/set', 'error', 'Invalid JSON', req_id)
                        return
                    path = str(data.get('path', '')).strip()
                    matte = str(data.get('matte', '')).strip() or 'none'
                    if not path:
                        self._publish_ack('slideshow/matte/set', 'error', 'Missing path', req_id)
                        return
                    if matte in ('', 'default', '__default__'):
                        self._matte_overrides.pop(path, None)
                        effective = self._resolve_matte_for(path, os.path.basename(path))
                    else:
                        self._matte_overrides[path] = matte
                        effective = matte
                    self._save_matte_overrides()
                    self._publish_matte_overrides()
                    self._publish_ack('slideshow/matte/set', 'ok',
                                      f'Matte saved ({effective}). Click "Apply now" to update the TV.',
                                      req_id, extra={'path': path, 'matte': effective})
                except Exception as e:
                    self._publish_ack('slideshow/matte/set', 'error', str(e), req_id)
                return
            if cmd == 'slideshow/matte/apply':
                # Make a previously-saved matte override visible on the TV by
                # deleting the image and re-uploading it with the new matte
                # baked into the send_image request. This is the only path
                # that produces a visible matte change on this firmware.
                try:
                    if not isinstance(data, dict):
                        self._publish_ack('slideshow/matte/apply', 'error', 'Invalid JSON', req_id)
                        return
                    path = str(data.get('path', '')).strip()
                    if not path:
                        self._publish_ack('slideshow/matte/apply', 'error', 'Missing path', req_id)
                        return
                    async def _apply_reupload():
                        try:
                            new_id, effective = await self._apply_matte_via_reupload(path)
                            self._publish_ack(
                                'slideshow/matte/apply', 'ok',
                                f'Matte {effective} applied',
                                req_id, extra={'path': path, 'matte': effective, 'content_id': new_id},
                            )
                        except _MatteRejectedError as ex:
                            self._publish_ack(
                                'slideshow/matte/apply', 'error',
                                f'TV rejected matte "{ex.matte}" for this image; pinned to none.',
                                req_id, extra={'path': path, 'matte': 'none'},
                            )
                        except Exception as ex:
                            self.log.warning('matte/apply failed for %s: %s', path, ex)
                            self._publish_ack('slideshow/matte/apply', 'error', str(ex), req_id, extra={'path': path})
                    self._schedule_command_coro(_apply_reupload(), 'slideshow/matte/apply')
                except Exception as e:
                    self._publish_ack('slideshow/matte/apply', 'error', str(e), req_id)
                return
            if cmd == 'slideshow/matte_options/request':
                fut = self._schedule_command_coro(self._publish_matte_options(), 'slideshow/matte_options/request')
                if fut:
                    self._publish_ack('slideshow/matte_options/request', 'ok', 'Matte options publishing', req_id)
                else:
                    self._publish_ack('slideshow/matte_options/request', 'error', 'Failed to schedule', req_id)
                return
            if cmd == 'slideshow/available/request':
                # Optional 'collections' field allows the UI to preview a staged
                # collection set in the grid without committing selected_collections.
                preview_cols = None
                if data and 'collections' in data and isinstance(data['collections'], list):
                    raw_cols = [str(c).strip() for c in data['collections'] if str(c).strip()]
                    preview_cols = [self._map_to_artwork_dir(c) or c for c in raw_cols]
                self._publish_slideshow_available(override_collections=preview_cols)
                self._publish_ack('slideshow/available/request', 'ok', 'Available list published', req_id)
                return
            if cmd == 'slideshow/preview/request':
                # Compute a random selection dry-run without uploading anything.
                # Optional 'collections' field overrides selected_collections for the preview.
                preview_cols = list(self.selected_collections)
                if data and 'collections' in data and isinstance(data['collections'], list):
                    raw_cols = [str(c).strip() for c in data['collections'] if str(c).strip()]
                    preview_cols = [self._map_to_artwork_dir(c) or c for c in raw_cols]
                max_n = self.max_uploads
                paths = self._preview_random_selection(preview_cols, max_n)
                try:
                    ack = {
                        'cmd': cmd,
                        'status': 'ok',
                        'message': f'Preview generated: {len(paths)} image(s)',
                        'paths': paths,
                    }
                    if req_id is not None:
                        ack['req_id'] = req_id
                    ack['selected_collections'] = self.selected_collections
                    self._mqtt.publish(
                        f'{self.mqtt_ack_prefix}/{cmd}',
                        json.dumps(ack, separators=(',', ':')),
                        qos=0,
                        retain=False,
                    )
                except Exception as e:
                    self._publish_ack(cmd, 'error', str(e), req_id)
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
                # Refresh the collections dropdown even when there are no git sources — the
                # user may have added/removed folders in the media root since last startup.
                try:
                    self._publish_collections_state()
                    self._publish_selected_collections_state()
                    self._publish_settings_state()
                except Exception:
                    pass
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

            if self.tv is None:
                self.log.info('settings/sync_collections: TV unavailable — skipping reseed, collections updated on disk only')
                self._publish_ack('collections/refresh', 'done', 'Collections updated. TV reseed will happen automatically once the TV is reachable.', req_id)
            else:
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
                self._csv_by_path = {}
                # Rebuild artist<->dir maps
                self._artist_to_dir = {}
                self._dir_to_artist = {}
                for row in reader:
                    key = (row.get('artwork_file') or '').strip()
                    if key:
                        self._csv_by_file[key] = row
                        dn = (row.get('artwork_dir') or '').strip()
                        if dn:
                            self._csv_by_path[f"{dn}/{key}"] = row
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
            # NOTE: We do NOT call change_matte here. On this firmware the
            # matte is baked into the rendered composite at upload time and
            # change_matte is metadata-only — it never triggers a re-render.
            # The displayed matte will be whatever was baked when the image
            # was last uploaded. To change a matte visibly, the user triggers
            # an explicit re-upload via the slideshow/matte/apply command.
            await self.tv.select_image(content_id)
            self.shown_content_ids.add(content_id)  # Mark as shown
            self.current_content_id = content_id
            await self.update_ha_selected_artwork(content_id)
        else:
            self.log.info('skipping art update, as new content_id: %s is the same', content_id)

    async def _apply_matte_via_reupload(self, path):
        '''Make a per-image matte override visible on the TV by deleting the
        existing content_id and re-uploading the image with the matte baked
        into the send_image request. This is the only path that actually
        produces a visible matte change on this firmware (change_matte alone
        is metadata-only and does not trigger a re-render).

        Raises:
          - _MatteRejectedError if the TV rejects the matte (-7); the override
            is automatically pinned to 'none' and the image is re-uploaded
            with no matte before the exception is raised.
          - RuntimeError on other failures (not in art mode, file missing,
            image not currently uploaded, empty upload response).
        '''
        if not self.tv.art_mode:
            raise RuntimeError('TV is not in art mode')
        # Locate the uploaded_files entry for this path
        info = None
        base = None
        for k, v in self.uploaded_files.items():
            if v.get('path_rel') == path or k == os.path.basename(path):
                info = v
                base = k
                break
        if not info:
            raise RuntimeError(f'Image is not currently uploaded: {path}')
        old_content_id = info.get('content_id')
        rel_path = info.get('path_rel') or path
        # Resolve the file on disk: try media_root + rel_path first, then
        # self.folder + basename for single-folder mode.
        abs_path = None
        if self.media_root:
            cand = os.path.join(self.media_root, rel_path)
            if os.path.isfile(cand):
                abs_path = cand
        if abs_path is None and self.folder:
            cand = os.path.join(self.folder, base)
            if os.path.isfile(cand):
                abs_path = cand
        if abs_path is None:
            raise RuntimeError(f'File not found on disk for path: {path}')
        matte = self._resolve_matte_for(rel_path, os.path.basename(rel_path)) or 'none'
        file_data, file_type = self.read_file(abs_path)
        if not file_data:
            raise RuntimeError(f'Failed to read file: {abs_path}')
        # If we're currently showing the image, switch to standby first so
        # the TV isn't left holding a stale reference during the delete.
        was_current = bool(old_content_id) and old_content_id == self.current_content_id
        if was_current and self.standby_content_id:
            try:
                await self.tv.select_image(self.standby_content_id)
                self.current_content_id = self.standby_content_id
            except Exception as ex:
                self.log.debug('matte apply: pre-delete standby select failed: %s', ex)
        # Delete the existing content_id
        if old_content_id:
            try:
                self.log.info('matte apply: deleting %s for %s', old_content_id, rel_path)
                await self.tv.delete_list([old_content_id])
            except Exception as ex:
                self.log.warning('matte apply: delete failed for %s (%s): %s', rel_path, old_content_id, ex)
        # Re-upload with the matte baked in
        self.log.info('matte apply: re-uploading %s with matte=%s', rel_path, matte)
        rejected = False
        try:
            new_content_id = await self._upload_to_tv(file_data, file_type, matte)
        except Exception as ex:
            if matte != 'none' and self._is_matte_minus_7(ex):
                rejected = True
                new_content_id = None
            else:
                raise
        # Silent-failure: empty content_id with a non-'none' matte means the
        # TV swallowed the request — treat as rejection.
        if new_content_id is None and not rejected and matte != 'none':
            rejected = True
        if rejected:
            self.log.warning('matte apply: TV rejected matte %r for %s; pinning to none and re-uploading',
                             matte, rel_path)
            self._matte_overrides[rel_path] = 'none'
            try:
                self._save_matte_overrides()
                self._publish_matte_overrides()
            except Exception:
                pass
            try:
                new_content_id = await self._upload_to_tv(file_data, file_type, 'none')
            except Exception as ex2:
                raise RuntimeError(f'fallback upload (matte=none) failed: {ex2}') from ex2
            if not new_content_id:
                raise RuntimeError('fallback upload (matte=none) returned no content_id')
            self.update_uploaded_files(base, new_content_id, full_path=abs_path)
            self.write_program_data()
            # Restore display if we were showing the image
            if was_current:
                try:
                    await self.tv.select_image(new_content_id)
                    self.current_content_id = new_content_id
                except Exception as ex:
                    self.log.debug('matte apply: post-reupload select failed: %s', ex)
            try:
                self._publish_slideshow_state()
            except Exception:
                pass
            raise _MatteRejectedError(matte)
        if not new_content_id:
            raise RuntimeError('upload returned no content_id')
        self.update_uploaded_files(base, new_content_id, full_path=abs_path)
        self.write_program_data()
        if was_current:
            try:
                await self.tv.select_image(new_content_id)
                self.current_content_id = new_content_id
            except Exception as ex:
                self.log.warning('matte apply: post-reupload select failed for %s: %s', rel_path, ex)
        try:
            self._publish_slideshow_state()
        except Exception:
            pass
        return new_content_id, matte

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
                # Distinguish "TV connection object is missing/dead" from a genuine
                # "TV reachable but art mode off". The former needs a reconnect; without
                # this the loop would spin forever calling art API methods on a None/dead
                # connection (see issue #11: 'NoneType' object has no attribute 'get_artmode').
                if self.tv is None or not self.tv.is_alive():
                    self.log.warning('TV connection unavailable; attempting reconnect (failure %d)', self.consecutive_failures)
                    self._not_in_artmode_logged = False
                    if await self.reconnect_tv():
                        self.log.info('TV connection restored; resuming monitoring')
                        self.consecutive_failures = 0
                        continue
                    # Reconnect failed — back off (capped) before trying again.
                    await asyncio.sleep(self.get_backoff_delay())
                    continue
                backoff_delay = self.artmode_refresh_seconds or 1
                if not self._not_in_artmode_logged:
                    self.log.info('TV is not in art mode')
                    self._not_in_artmode_logged = True
                else:
                    self.log.debug('TV is not in art mode')
                if self._artmode_event:
                    self._artmode_event.clear()
                    try:
                        await asyncio.wait_for(self._artmode_event.wait(), timeout=backoff_delay)
                        self.log.debug('Art mode event received — rechecking immediately')
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(backoff_delay)
                continue
            self._not_in_artmode_logged = False
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
            if self._artmode_event:
                self._artmode_event.clear()
                try:
                    await asyncio.wait_for(self._artmode_event.wait(), timeout=self.period)
                    self.log.debug('Art mode event received during idle — rechecking')
                except asyncio.TimeoutError:
                    pass
            else:
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
                    if len(parts) > 1:
                        collection = os.path.join(*parts[:-1])
                    elif parts:
                        collection = parts[0]
            except Exception:
                collection = None
        else:
            # Unknown content (e.g., selected outside uploader); publish sentinel values
            if not self.current_content_id:
                display = 'Standby'
            elif self.standby_content_id and self.current_content_id == self.standby_content_id:
                # TV is showing standby — report it correctly instead of 'Unknown'
                display = 'Standby'
                filename = 'standby.png'
            else:
                display = 'Unknown'
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
        self._publish_slideshow_state()
        try:
            def _on_upload_progress(idx, total, name):
                ack('progress', f'Uploading {idx}/{total}: {os.path.basename(name)}')
            self._reseed_progress_cb = _on_upload_progress

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
                    self.log.warning('Failed to select standby before cleanup: %s — invalidating standby_content_id so it will be re-uploaded next run', e)
                    self.standby_content_id = None

            # Snapshot the current selection so the next pick avoids repeating the same images
            self._last_slideshow_paths = {
                v.get('path_rel') for v in self.uploaded_files.values() if v.get('path_rel')
            }

            # Count existing uploads before cleanup for the progress message
            _existing = len([k for k in self.uploaded_files if k not in self.exclude and k != (os.path.basename(self.standby) if self.standby else None)])
            ack('progress', f'Removing {_existing} old upload(s) from TV...' if _existing else 'Removing old uploads from TV...')
            await self.cleanup_old_uploads()

            # Re-select standby after cleanup: the TV can revert to the last playing art
            # (e.g. the previous override image) during the deletion pass, then fall back
            # to its built-in default when that image is also deleted.  Re-pinning here
            # ensures we hold standby for the entire upload phase.
            if self.standby_content_id:
                try:
                    await self.tv.select_image(self.standby_content_id)
                    self.log.info('Standby re-selected after cleanup: %s', self.standby_content_id)
                except Exception as e:
                    self.log.warning('Failed to re-select standby after cleanup: %s', e)

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
            self._reseed_progress_cb = None
            self._refresh_in_progress = False
            self._publish_slideshow_state()
            self._publish_slideshow_available()
            # Force-publish current artwork state so UIs clear any stale in_art_mode=false
            # retained from a previous session and correctly re-enable buttons.
            try:
                await self._publish_current_artwork_state(force=True)
            except Exception:
                pass

    async def _do_collections_refresh(self, req_id=None):
        """MQTT-triggered refresh: clears slideshow override then reseeds."""
        if self.slideshow_override is not None:
            self.slideshow_override = None
            self._save_slideshow_override()
            self._publish_slideshow_state()
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
