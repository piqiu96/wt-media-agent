import base64
import struct
import unittest

from wt_media_agent.clients.bilibili.cover import cover_upload_kind


class CoverExportTests(unittest.TestCase):
    def request(self, data, mime='image/png', repeat=False):
        part = b'--proof\r\nContent-Disposition: form-data; name="cover"\r\n\r\n'
        part += b'data:' + mime.encode() + b';base64,' + base64.b64encode(data) + b'\r\n'
        return 'multipart/form-data; boundary=proof', part * (2 if repeat else 1) + b'--proof--\r\n'

    def png(self, width, height):
        return b'\x89PNG\r\n\x1a\n' + struct.pack('!I', 13) + b'IHDR' + struct.pack('!II', width, height)

    def test_binds_each_export_to_its_distinct_ratio(self):
        for dimensions, expected in [((1200, 900), 'cover43'), ((1920, 1080), 'cover')]:
            self.assertEqual(cover_upload_kind(*self.request(self.png(*dimensions))), expected)
        jpeg = b'\xff\xd8\xff\xc0\x00\x0b\x08' + struct.pack('!HH', 1080, 1920) + b'\x01\x01\x11\x00'
        self.assertEqual(cover_upload_kind(*self.request(jpeg, 'image/jpeg')), 'cover')

    def test_rejects_missing_duplicate_malformed_or_wrong_ratio_export(self):
        for args in [self.request(self.png(1200, 900), repeat=True), self.request(self.png(900, 1200)),
                     self.request(b'not an image'), ('application/json', b'{}'),
                     ('multipart/form-data; boundary=proof', b'--proof--\r\n')]:
            with self.subTest(args=args[:1]), self.assertRaises(ValueError):
                cover_upload_kind(*args)
