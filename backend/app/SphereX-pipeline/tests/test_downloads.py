import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import downloads


class Response:
    def __init__(self, status, headers, body):
        self.status_code, self.headers, self.body = status, headers, body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.body


class DownloadTests(unittest.TestCase):
    body = bytes(range(256))*137

    def get(self, url, headers, **kwargs):
        if 'Range' not in headers:
            return Response(200, {'Content-Length': str(len(self.body))}, self.body)
        start, end = map(int, headers['Range'].removeprefix('bytes=').split('-'))
        return Response(206, {'Content-Range': f'bytes {start}-{end}/{len(self.body)}',
                              'ETag': '"version1"'}, self.body[start:end+1])

    def test_parallel_exact_bytes_and_cache(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(downloads, 'is_url_in_cache', return_value=False), \
                patch.object(downloads.requests, 'get', side_effect=self.get) as get:
            result = downloads.fetch('https://example.test/cube.fits', root, workers=4)
            self.assertEqual(result.read_bytes(), self.body)
            self.assertEqual(get.call_count, 5)
            self.assertEqual(downloads.fetch('https://example.test/cube.fits', root), result)
            self.assertEqual(get.call_count, 5)

    def test_ignored_ranges_fallback(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(downloads, 'is_url_in_cache', return_value=False), \
                patch.object(downloads.requests, 'get', return_value=Response(
                    200, {'Content-Length': str(len(self.body))}, self.body)):
            self.assertEqual(downloads.fetch('https://example.test/cube.fits', root).read_bytes(), self.body)

    def test_bad_ranges_fallback(self):
        def get(url, headers, **kwargs):
            response = self.get(url, headers, **kwargs)
            if headers.get('Range', 'bytes=0-0') != 'bytes=0-0':
                response.headers['Content-Range'] = 'bytes 0-0/1'
            return response
        with tempfile.TemporaryDirectory() as root, \
                patch.object(downloads, 'is_url_in_cache', return_value=False), \
                patch.object(downloads.requests, 'get', side_effect=get):
            self.assertEqual(downloads.fetch('https://example.test/cube.fits', root).read_bytes(), self.body)

    def test_truncated_download_not_cached(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(downloads, 'is_url_in_cache', return_value=False), \
                patch.object(downloads.requests, 'get', return_value=Response(
                    200, {'Content-Length': '100'}, b'short')):
            with self.assertRaisesRegex(OSError, 'incompleta'):
                downloads.fetch('https://example.test/cube.fits', root)
            self.assertEqual(list(Path(root).iterdir()), [])
