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

    def test_cancelled_identity_before_connect_never_reads_or_replaces_file(self):
        with patch('wt_media_agent.services.files.verified_download.open_source') as source:
            with self.assertRaises(ResourceVerificationError):
                download_verified(self.grant,self.target,current=lambda:False)
            source.assert_not_called()
            self.assertFalse(self.target.exists())

    def test_slow_drip_exceeding_total_deadline_removes_part_and_keeps_original(self):
        self.target.parent.mkdir();self.target.write_bytes(b'previous')
        now=[10.0]
        class SlowStream(io.BytesIO):
            def read(stream,size):
                now[0]+=2
                return super(SlowStream,stream).read(1)
        with patch('wt_media_agent.services.files.verified_download.time.monotonic',side_effect=lambda:now[0]), \
             patch('wt_media_agent.services.files.verified_download.open_source',return_value=SlowStream(self.body)):
            with self.assertRaises(ResourceVerificationError):download_verified(self.grant,self.target,deadline=13.0)
        self.assertEqual(self.target.read_bytes(),b'previous')
        self.assertFalse(self.target.with_suffix('.part').exists())

    def test_real_http_slow_drip_obeys_total_deadline_before_body_finishes(self):
        import http.client
        import socket
        import threading
        import time
        from wt_media_agent.clients.transfer.source import SourceStream
        receiver, sender = socket.socketpair()
        self.addCleanup(receiver.close); self.addCleanup(sender.close)
        receiver.settimeout(.06)
        sender.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n')
        response = http.client.HTTPResponse(receiver); response.begin()
        stream = SourceStream(response=response, url_length=100, range_honoured=True, start_offset=0)
        stopped = threading.Event()
        def drip():
            try:
                for _ in range(100):
                    if stopped.wait(.01): return
                    sender.sendall(b'a')
            except OSError:
                pass
        worker = threading.Thread(target=drip, daemon=True); worker.start()
        self.addCleanup(stopped.set)
        grant = dict(self.grant,size=100,sha256=hashlib.sha256(b'a'*100).hexdigest())
        started = time.monotonic()
        try:
            with patch('wt_media_agent.services.files.verified_download.open_source',return_value=stream):
                with self.assertRaises(ResourceVerificationError):
                    download_verified(grant,self.target,deadline=started+.12)
        finally:
            stopped.set(); worker.join(1)
        self.assertLess(time.monotonic()-started,.5)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix('.part').exists())

    def test_identity_loss_after_chunk_never_commits_completed_download(self):
        active=[True]
        class SwitchingStream(io.BytesIO):
            def read(stream,size):
                chunk=super(SwitchingStream,stream).read(size);active[0]=False;return chunk
        with patch('wt_media_agent.services.files.verified_download.open_source',return_value=SwitchingStream(self.body)):
            with self.assertRaises(ResourceVerificationError):download_verified(self.grant,self.target,current=lambda:active[0])
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix('.part').exists())
