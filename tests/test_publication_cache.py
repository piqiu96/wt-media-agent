"""Real SQLite/filesystem checks; receipts are fixtures, never platform proof."""
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import PublicationJournal
from wt_media_agent.runner.publication_cache import PublicationCacheCleanup
from test_publication_execution import identity, snapshot

class PublicationCacheTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'db';apply_migrations(self.db)
        self.j=PublicationJournal(Database(self.db));self.i=identity()
        self.cache=self.root/'publications';self.folder=self.cache/self.i.key
        self.folder.mkdir(parents=True)
        for name in ('video.mp4','cover','portrait-cover','video.mp4.part'):(self.folder/name).write_bytes(b'fixture')
        self.calls=[]
        self.original={'attempt_id':self.i.attempt_id,'result_hash':None,'scene_resolved':True,'resolved_status':'reviewing'}
        class Cloud:
            def original_publication_attempt(_,token,task,attempt):
                self.calls.append((task,attempt));return dict(self.original)
        self.cloud=Cloud();self.current=True
        self.cleaner=self.new_cleaner()
    def new_cleaner(self):
        return PublicationCacheCleanup(self.j,self.cloud,self.cache,authorized=lambda token,user:self.current)
    def settled(self, *, status='reviewing', closed=True, delivered=True):
        self.j.register(self.i,snapshot())
        packet=self.j.save_result(self.i,{'status':status,'platform_work_id':'123','evidence':{'scene_closed':closed,'receipt_id':'123'}},report_seq=1)
        self.original['result_hash']=packet['result_hash']
        if delivered:self.j.record_delivery('dev','3',packet,accepted=True)
        if delivered and closed:self.j.settle(self.i,packet['result_hash'],task_status=status)
        return packet
    def test_acknowledged_closed_receipt_is_removed_without_erasing_evidence(self):
        self.settled();self.cleaner.clean('token',self.i)
        self.assertFalse(self.folder.exists())
        state=self.j.get(self.i)
        self.assertEqual(state['cache_cleanup']['status'],'complete')
        self.assertEqual(state['file_digests']['video_sha256'],snapshot()['video_sha256'])
        self.assertEqual(state['phase'],'settled')
        self.assertEqual(self.j.pending('dev','3'),[])
    def test_unacknowledged_or_open_scene_or_failed_result_is_retained(self):
        for options in ({'delivered':False},{'closed':False},{'status':'failed'}):
            with self.subTest(options=options):
                self.setUp();self.settled(**options);self.cleaner.clean('token',self.i)
                self.assertTrue((self.folder/'video.mp4').exists())
    def test_recovered_success_with_old_open_scene_rechecks_the_exact_attempt(self):
        packet = self.settled(closed=False)
        self.j.settle(self.i, packet['result_hash'], task_status='published')
        self.original['resolved_status'] = 'published'
        self.original['scene_resolved'] = False
        self.cleaner.sweep('token', 'dev', '3')
        self.assertTrue(self.folder.exists())
        self.original['scene_resolved'] = True
        self.new_cleaner().sweep('token', 'dev', '3')
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.j.get(self.i)['scene_resolution']['scene_resolved'], True)
        self.assertTrue(self.calls)

    def test_failed_delete_retries_after_process_restart(self):
        self.settled()
        with patch.object(self.cleaner, '_remove', side_effect=PermissionError):
            self.cleaner.clean('token',self.i)
        self.assertEqual(self.j.get(self.i)['cache_cleanup']['status'],'failed')
        self.new_cleaner().sweep('token','dev','3')
        self.assertFalse(self.folder.exists())
    def test_missing_folder_is_idempotent(self):
        self.settled();self.cleaner.clean('token',self.i);self.cleaner.clean('token',self.i)
        self.assertEqual(self.j.get(self.i)['cache_cleanup']['status'],'complete')
    def test_crash_after_delete_before_completion_record_recovers(self):
        self.settled()
        record = self.j.record_cache_cleanup
        def fail_complete(identity, status):
            if status == 'complete':
                raise RuntimeError('interrupted after deletion')
            return record(identity, status)
        with patch.object(self.j, 'record_cache_cleanup', side_effect=fail_complete):
            self.cleaner.clean('token',self.i)
        self.assertFalse(self.folder.exists())
        self.new_cleaner().sweep('token','dev','3')
        self.assertEqual(self.j.get(self.i)['cache_cleanup']['status'],'complete')
    def test_symlink_and_unknown_entries_are_not_deleted(self):
        self.settled();outside=self.root/'keep';outside.write_bytes(b'keep')
        (self.folder/'extra').symlink_to(outside)
        self.cleaner.clean('token',self.i)
        self.assertEqual(outside.read_bytes(),b'keep');self.assertTrue((self.folder/'video.mp4').exists())
        (self.folder/'extra').unlink();(self.folder/'unknown').write_bytes(b'keep')
        self.cleaner.clean('token',self.i);self.assertTrue((self.folder/'unknown').exists())
    def test_linked_attempt_directory_cannot_escape_cache(self):
        self.settled();renamed=self.root/'keep';self.folder.rename(renamed);self.folder.symlink_to(renamed,target_is_directory=True)
        self.cleaner.clean('token',self.i);self.assertTrue((renamed/'video.mp4').exists())
    def test_wrong_user_environment_or_revoked_identity_cannot_clean(self):
        self.settled();self.cleaner.sweep('token','prod','3');self.cleaner.sweep('token','dev','4')
        self.assertTrue(self.folder.exists());self.current=False;self.cleaner.clean('token',self.i)
        self.assertTrue(self.folder.exists())
    def test_legacy_settled_record_requires_exact_cloud_confirmation(self):
        self.settled()
        with self.j.database.session.begin() as session:
            from sqlalchemy import select
            from wt_media_agent.storage.models import TaskCheckpointModel
            import json
            row=session.scalar(select(TaskCheckpointModel));state=json.loads(row.checkpoint_json)
            state.pop('resolved_status',None);row.checkpoint_json=json.dumps(state)
        self.original['attempt_id']='999';self.cleaner.clean('token',self.i);self.assertTrue(self.folder.exists())
        self.original['attempt_id']=self.i.attempt_id;self.new_cleaner().sweep('token','dev','3')
        self.assertFalse(self.folder.exists());self.assertTrue(self.calls)
    def test_newer_attempt_does_not_authorize_old_failed_cache(self):
        self.settled(status='failed')
        newer=replace(self.i,attempt_id='2');self.j.register(newer,snapshot())
        packet=self.j.save_result(newer,{'status':'reviewing','evidence':{'scene_closed':True}},report_seq=1)
        self.j.record_delivery('dev','3',packet,accepted=True);self.j.settle(newer,packet['result_hash'],task_status='reviewing')
        self.cleaner.sweep('token','dev','3');self.assertTrue(self.folder.exists())

    def test_active_attempt_and_other_attempt_files_are_preserved(self):
        self.j.register(self.i, snapshot())
        self.cleaner.sweep('token', 'dev', '3')
        self.assertTrue(self.folder.exists())
        packet = self.j.save_result(self.i, {'status': 'reviewing', 'evidence': {'scene_closed': True}}, report_seq=1)
        self.j.record_delivery('dev', '3', packet, accepted=True)
        self.j.settle(self.i, packet['result_hash'], task_status='reviewing')
        other = self.cache / replace(self.i, attempt_id='2').key
        other.mkdir(); (other / 'video.mp4').write_bytes(b'other attempt')
        self.cleaner.clean('token', self.i)
        self.assertEqual((other / 'video.mp4').read_bytes(), b'other attempt')

    def test_linked_cache_root_is_rejected(self):
        self.settled()
        moved = self.root / 'keep-publications'
        self.cache.rename(moved)
        self.cache.symlink_to(moved, target_is_directory=True)
        self.cleaner.clean('token', self.i)
        self.assertTrue((moved / self.i.key / 'video.mp4').exists())

    def test_sweep_is_immediate_then_throttled_for_ten_minutes(self):
        self.settled()
        with patch.object(self.cleaner, '_remove', side_effect=PermissionError) as remove:
            with patch('wt_media_agent.runner.publication_cache.monotonic', side_effect=[0, 599, 600]):
                self.cleaner.sweep('token', 'dev', '3')
                self.cleaner.sweep('token', 'dev', '3')
                self.assertEqual(remove.call_count, 1)
                self.cleaner.sweep('token', 'dev', '3')
                self.assertEqual(remove.call_count, 2)

    def test_parent_replacement_during_unlink_cannot_redirect_deletion(self):
        if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
            self.skipTest('descriptor-relative deletion is POSIX-specific')
        self.settled()
        outside = self.root / 'outside'
        target = outside / self.i.key
        target.mkdir(parents=True)
        (target / 'video.mp4').write_bytes(b'keep')
        unlink = os.unlink
        replaced = False
        def replace_parent(name, *, dir_fd=None):
            nonlocal replaced
            if not replaced:
                replaced = True
                self.cache.rename(self.root / 'moved-cache')
                self.cache.symlink_to(outside, target_is_directory=True)
            return unlink(name, dir_fd=dir_fd)
        with patch('wt_media_agent.runner.publication_cache.os.unlink', side_effect=replace_parent):
            self.cleaner.clean('token', self.i)
        self.assertEqual((target / 'video.mp4').read_bytes(), b'keep')
        self.assertFalse((self.root / 'moved-cache' / self.i.key).exists())

    def test_legacy_wrong_hash_unresolved_or_unavailable_cloud_is_retained(self):
        self.settled()
        with self.j.database.session.begin() as session:
            from sqlalchemy import select
            from wt_media_agent.storage.models import TaskCheckpointModel
            import json
            row = session.scalar(select(TaskCheckpointModel))
            state = json.loads(row.checkpoint_json)
            state.pop('resolved_status')
            row.checkpoint_json = json.dumps(state)
        original = dict(self.original)
        for change in ({'result_hash': 'mismatch'}, {'scene_resolved': False}, {'resolved_status': 'uncertain'}, {'resolved_status': 'failed'}):
            with self.subTest(change=change):
                self.original = dict(original, **change)
                self.cleaner.clean('token', self.i)
                self.assertTrue(self.folder.exists())
        # Recreate a legacy view after the valid failed resolution above.
        with self.j.database.session.begin() as session:
            row = session.scalar(select(TaskCheckpointModel))
            state = json.loads(row.checkpoint_json)
            state.pop('scene_resolution', None)
            row.checkpoint_json = json.dumps(state)
        with patch.object(self.cloud, 'original_publication_attempt', side_effect=ConnectionError):
            self.cleaner.clean('token', self.i)
        self.assertTrue(self.folder.exists())

    def test_portable_non_recursive_removal_keeps_other_directories(self):
        self.settled()
        other = self.cache / 'user-downloads'
        other.mkdir(); (other / 'video.mp4').write_bytes(b'keep')
        with patch('wt_media_agent.runner.publication_cache.os.supports_dir_fd', set()):
            self.cleaner.clean('token', self.i)
        self.assertFalse(self.folder.exists())
        self.assertEqual((other / 'video.mp4').read_bytes(), b'keep')
        self.assertEqual(self.j.get(self.i)['cache_cleanup']['status'], 'complete')
