"""Actual immutable file checks, with only the basic network client replaced."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wt_media_agent.services.files.verified_download import download_verified, ResourceVerificationError


class VerifiedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / 'private' / 'cover'
        self.body = b'original frozen bytes'
        self.grant = {'url': 'https://store.test/object?signature=private',
                      'size': len(self.body), 'sha256': hashlib.sha256(self.body).hexdigest()}

    def download(self, body):
        with patch('wt_media_agent.services.files.verified_download.open_source', return_value=io.BytesIO(body)):
            download_verified(self.grant, self.target)

    def test_exact_original_bytes_are_saved_with_private_permissions(self):
        self.download(self.body)
        self.assertEqual(self.target.read_bytes(), self.body)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.target.with_suffix('.part').exists())

    def test_wrong_digest_short_and_oversize_content_never_replace_original(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'previous')
        for body in (b'X' * len(self.body), self.body[:-1], self.body + b'X'):
            with self.subTest(body=body), self.assertRaises(ResourceVerificationError): self.download(body)
            self.assertEqual(self.target.read_bytes(), b'previous')
            self.assertFalse(self.target.with_suffix('.part').exists())

    def test_invalid_grants_do_not_open_network_or_create_files(self):
        for changes in ({'url': 'http://store.test/object'}, {'size': True}, {'sha256': 'invalid'}):
            with self.subTest(changes=changes), patch('wt_media_agent.services.files.verified_download.open_source') as source:
                with self.assertRaises(ResourceVerificationError): download_verified(dict(self.grant, **changes), self.target)
                source.assert_not_called()
                self.assertFalse(self.target.exists())

    def test_private_upstream_error_is_not_exposed(self):
        with patch('wt_media_agent.services.files.verified_download.open_source', side_effect=RuntimeError(self.grant['url'])):
            with self.assertRaises(ResourceVerificationError) as error: download_verified(self.grant, self.target)
        self.assertNotIn('signature', str(error.exception))
        self.assertFalse(self.target.with_suffix('.part').exists())
