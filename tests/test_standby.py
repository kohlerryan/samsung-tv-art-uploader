"""Standby content-id validation and persistence in uploader.py.

Drives the real monitor_and_display against an in-memory stand-in for the TV, so it
needs neither a Frame TV nor the TV library. Run from the repo root:

    python -m unittest discover -s tests -v
"""
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uploader  # noqa: E402

STANDBY_DATE = '2026:11:20 09:00:00'
PAINTING_DATE = '2026:11:24 13:43:00'


def photo(content_id, image_date=None):
    entry = {'content_id': content_id, 'category_id': 'MY-C0002'}
    if image_date is not None:
        entry['image_date'] = image_date
    return entry


class FakeTV:
    """The parts of SamsungTVAsyncArt the standby code touches."""
    art_mode = True

    def __init__(self, photos=(), next_num=2662):
        self.photos = [dict(p) for p in photos]
        self.next_num = next_num
        self.current = None
        self.uploads = []
        self.deleted = []
        self.available_calls = 0
        self.fail_available = False     # every My Photos query fails
        self.hide_new_for_queries = 0   # a new upload is missing from the list for N queries
        self._hidden = {}

    async def available(self, category=None, timeout=10):
        self.available_calls += 1
        if self.fail_available:
            raise RuntimeError('TV did not answer the art request')
        visible = []
        for p in self.photos:
            if self._hidden.get(p['content_id'], 0) > 0:
                self._hidden[p['content_id']] -= 1
                continue
            visible.append(dict(p))
        return [p for p in visible if p['category_id'] == category]

    async def upload(self, file_data, file_type='png', matte='none', portrait_matte='none'):
        content_id = 'MY_F%04d' % self.next_num
        self.next_num += 1
        self.photos.append(photo(content_id, '2026:11:24 15:30:%02d' % len(self.uploads)))
        self._hidden[content_id] = self.hide_new_for_queries
        self.uploads.append(content_id)
        return content_id

    async def select_image(self, content_id, category=None, show=True):
        self.current = content_id

    async def get_current(self):
        return {'content_id': self.current}

    async def delete_list(self, content_ids):
        self.deleted.extend(content_ids)
        self.photos = [p for p in self.photos if p['content_id'] not in content_ids]

    def ids(self):
        return [p['content_id'] for p in self.photos]


class StandbyContentIdTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.media = os.path.join(tmp.name, 'media')
        os.makedirs(self.media)
        with open(os.path.join(self.media, 'standby.png'), 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        self.cache_path = os.path.join(tmp.name, 'uploaded_files_cache.json')

        env = mock.patch.dict(os.environ, {
            'SAMSUNG_TV_ART_CACHE_FILE': self.cache_path,
            'SAMSUNG_TV_ART_MEDIA_ROOT': self.media,
        })
        env.start()
        self.addCleanup(env.stop)
        for name in ('SAMSUNG_TV_ART_MQTT_HOST', 'SAMSUNG_TV_ART_MQTT_DISCOVERY'):
            os.environ.pop(name, None)

        self.sleeps = []

        async def no_sleep(seconds, *args, **kwargs):
            self.sleeps.append(seconds)
        sleep = mock.patch.object(uploader.asyncio, 'sleep', no_sleep)
        sleep.start()
        self.addCleanup(sleep.stop)

    def write_cache(self, **entries):
        with open(self.cache_path, 'w') as f:
            json.dump(entries, f)

    def read_cache(self):
        if not os.path.isfile(self.cache_path):
            return {}
        with open(self.cache_path) as f:
            return json.load(f)

    def start(self, tv):
        """A fresh process: nothing in memory, same cache file and TV."""
        mon = uploader.monitor_and_display('192.0.2.1', self.media, standby='standby.png')
        mon.tv = tv

        async def in_artmode():
            return True
        mon.safe_in_artmode = in_artmode
        return mon

    async def ensure(self, mon):
        with self.assertLogs('Main', level='DEBUG') as logs:
            await mon.ensure_standby_selected()
        return [r.getMessage() for r in logs.records if r.levelno >= logging.WARNING]

    def rejection(self, warnings, content_id):
        hits = [w for w in warnings if content_id in w and 'stale' in w]
        self.assertEqual(len(hits), 1, 'expected one warning rejecting %s, got %s' % (content_id, warnings))
        return hits[0]

    async def test_id_without_stored_date_is_rejected_even_though_tv_lists_it(self):
        self.write_cache(_standby_content_id='MY_F2661', _standby_image_date=None)
        tv = FakeTV([photo('MY_F2661', PAINTING_DATE)])
        mon = self.start(tv)

        warnings = await self.ensure(mon)

        self.assertIn('no image_date', self.rejection(warnings, 'MY_F2661'))
        self.assertEqual(tv.uploads, ['MY_F2662'])
        self.assertEqual(tv.current, 'MY_F2662')
        self.assertEqual(mon.standby_content_id, 'MY_F2662')
        cache = self.read_cache()
        self.assertEqual(cache['_standby_content_id'], 'MY_F2662')
        self.assertTrue(cache['_standby_image_date'])

    async def test_date_mismatch_is_rejected(self):
        self.write_cache(_standby_content_id='MY_F2661', _standby_image_date=STANDBY_DATE)
        tv = FakeTV([photo('MY_F2661', PAINTING_DATE)])
        mon = self.start(tv)

        warnings = await self.ensure(mon)

        message = self.rejection(warnings, 'MY_F2661')
        self.assertIn(STANDBY_DATE, message)
        self.assertIn(PAINTING_DATE, message)
        self.assertEqual(tv.uploads, ['MY_F2662'])
        self.assertEqual(self.read_cache()['_standby_content_id'], 'MY_F2662')

    async def test_id_absent_from_tv_is_rejected(self):
        self.write_cache(_standby_content_id='MY_F2661', _standby_image_date=STANDBY_DATE)
        tv = FakeTV([photo('MY_F2600', PAINTING_DATE)])
        mon = self.start(tv)

        warnings = await self.ensure(mon)

        self.assertIn('no longer on the TV', self.rejection(warnings, 'MY_F2661'))
        self.assertEqual(tv.uploads, ['MY_F2662'])

    async def test_failed_tv_query_keeps_a_dated_id(self):
        self.write_cache(_standby_content_id='MY_F2661', _standby_image_date=STANDBY_DATE)
        tv = FakeTV([photo('MY_F2661', STANDBY_DATE)])
        tv.fail_available = True
        mon = self.start(tv)

        warnings = await self.ensure(mon)

        self.assertEqual([w for w in warnings if 'stale' in w], [])
        self.assertEqual(tv.uploads, [])
        self.assertEqual((mon.standby_content_id, mon.standby_image_date), ('MY_F2661', STANDBY_DATE))
        cache = self.read_cache()
        self.assertEqual(cache['_standby_content_id'], 'MY_F2661')
        self.assertEqual(cache['_standby_image_date'], STANDBY_DATE)

    async def test_matching_id_and_date_is_reused(self):
        self.write_cache(_standby_content_id='MY_F2661', _standby_image_date=STANDBY_DATE)
        tv = FakeTV([photo('MY_F2661', STANDBY_DATE)])
        mon = self.start(tv)

        warnings = await self.ensure(mon)

        self.assertEqual(warnings, [])
        self.assertEqual(tv.uploads, [])
        self.assertEqual(mon.standby_content_id, 'MY_F2661')

    async def test_upload_retries_until_the_tv_lists_the_date(self):
        tv = FakeTV()
        tv.hide_new_for_queries = 2
        mon = self.start(tv)

        await self.ensure(mon)

        self.assertEqual(tv.available_calls, 3)
        cache = self.read_cache()
        self.assertEqual(cache['_standby_content_id'], 'MY_F2662')
        self.assertTrue(cache['_standby_image_date'])

    async def test_upload_without_a_date_is_used_but_never_persisted(self):
        tv = FakeTV()
        tv.fail_available = True
        mon = self.start(tv)

        warnings = await self.ensure(mon)

        self.assertEqual(tv.current, 'MY_F2662')
        self.assertEqual(mon.standby_content_id, 'MY_F2662')
        self.assertTrue(any('image_date' in w and 'MY_F2662' in w for w in warnings))
        self.assertLessEqual(sum(self.sleeps), 10, 'the date retry should be brief')
        self.assertIsNone(self.read_cache().get('_standby_content_id'))

        await mon.cleanup_old_uploads()   # re-persists the standby id after wiping the cache
        self.assertIsNone(self.read_cache().get('_standby_content_id'))

    async def test_undated_cache_self_heals_and_later_starts_do_not_reupload(self):
        self.write_cache(_standby_content_id='MY_F2661', _standby_image_date=None)
        tv = FakeTV([photo('MY_F2655', '2026:11:24 13:42:10'), photo('MY_F2661', PAINTING_DATE)])
        tv.current = 'MY_F2661'           # a painting is on screen, reported as standby

        mon = self.start(tv)
        self.rejection(await self.ensure(mon), 'MY_F2661')
        await mon.cleanup_old_uploads()

        self.assertEqual(tv.ids(), ['MY_F2662'], 'the image holding the old id must not stay protected')
        self.assertEqual(tv.current, 'MY_F2662')
        healed = self.read_cache()
        self.assertEqual(healed['_standby_content_id'], 'MY_F2662')
        self.assertTrue(healed['_standby_image_date'])

        for _ in range(2):
            mon = self.start(tv)
            self.assertEqual(await self.ensure(mon), [])
            await mon.cleanup_old_uploads()
            self.assertEqual(tv.uploads, ['MY_F2662'])
            cache = self.read_cache()
            self.assertEqual(cache['_standby_content_id'], 'MY_F2662')
            self.assertEqual(cache['_standby_image_date'], healed['_standby_image_date'])


if __name__ == '__main__':
    unittest.main()
