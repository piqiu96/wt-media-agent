"""Remove only confirmed, settled publication downloads; never platform state."""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from time import monotonic

logger = logging.getLogger(__name__)
_FILES = frozenset({'video.mp4', 'cover', 'portrait-cover', 'space-cover'})
# Only exact filenames produced by the platform adapters belong to this
# attempt. Unknown entries and links still prevent all removal.
_AUXILIARY = {
    *(name + '-upload.' + suffix for name in ('cover', 'portrait-cover', 'space-cover')
      for suffix in ('png', 'jpg', 'webp')),
    *(name + '.' + suffix for name in ('editor-failure', 'cover-selection-0', 'cover-selection-1')
      for suffix in ('png', 'json')),
}
_ALLOWED = _FILES | {name + '.part' for name in _FILES} | _AUXILIARY


class PublicationCacheCleanup:
    def __init__(self, journal, cloud, directory, *, authorized, interval=600):
        if interval <= 0:
            raise ValueError('cleanup interval must be positive')
        self.journal = journal
        self.cloud = cloud
        self.directory = Path(directory)
        self.authorized = authorized
        self.interval = interval
        self._last_sweep = {}

    @staticmethod
    def _linked(path):
        return path.is_symlink() or path.is_junction()

    def _remove(self, identity):
        root = self.directory
        folder = root / identity.key
        # The basename comes only from a validated execution identity. Never
        # recurse: an unexpected entry must keep the directory for inspection.
        if self._linked(root) or self._linked(folder):
            raise ValueError('linked publication cache')
        if not folder.exists():
            return
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            self._remove_relative(root, identity.key)
            return
        # Windows has no dir_fd operations. Unlinking regular files and rmdir
        # never traverse child directories or junctions, unlike recursive delete.
        entries = list(folder.iterdir())
        for entry in entries:
            if entry.name not in _ALLOWED or self._linked(entry) or not entry.is_file():
                raise ValueError('unrecognized publication cache entry')
        for entry in entries:
            if self._linked(root) or self._linked(folder) or self._linked(entry):
                raise ValueError('linked publication cache')
            entry.unlink(missing_ok=True)
        folder.rmdir()

    @staticmethod
    def _remove_relative(root, name):
        # Pin both directories so a concurrent rename/link replacement cannot
        # redirect deletion into another task or a user-managed directory.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(root, flags)
        try:
            try:
                folder_fd = os.open(name, flags, dir_fd=root_fd)
            except FileNotFoundError:
                return
            try:
                entries = os.listdir(folder_fd)
                for entry in entries:
                    mode = os.stat(entry, dir_fd=folder_fd, follow_symlinks=False).st_mode
                    if entry not in _ALLOWED or not stat.S_ISREG(mode):
                        raise ValueError('unrecognized publication cache entry')
                for entry in entries:
                    try:
                        os.unlink(entry, dir_fd=folder_fd)
                    except FileNotFoundError:
                        pass
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                opened = os.fstat(folder_fd)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError('replaced publication cache directory')
                os.rmdir(name, dir_fd=root_fd)
            finally:
                os.close(folder_fd)
        finally:
            os.close(root_fd)

    def clean(self, token, identity):
        try:
            if not self.authorized(token, identity.user_id):
                return
            state = self.journal.cache_cleanup_state(identity)
            if state['eligibility'] == 'confirm':
                original = self.cloud.original_publication_attempt(token, identity.task_id, identity.attempt_id)
                if not self.authorized(token, identity.user_id):
                    return
                self.journal.settle_resolved(identity, original)
                state = self.journal.cache_cleanup_state(identity)
            if state['eligibility'] != 'ready':
                return
            self.journal.record_cache_cleanup(identity, 'pending')
            if not self.authorized(token, identity.user_id):
                return
            self._remove(identity)
            self.journal.record_cache_cleanup(identity, 'complete')
        except Exception as error:
            # Cleanup has its own retry state. Never rewrite the execution
            # result/outbox or propagate a delete error into publication work.
            try:
                self.journal.record_cache_cleanup(identity, 'failed')
            except Exception:
                pass
            logger.warning('publication cache cleanup deferred: task=%s attempt=%s error_type=%s',
                           identity.task_id, identity.attempt_id, type(error).__name__)

    def sweep(self, token, cloud_env, user_id):
        if not self.authorized(token, user_id):
            return
        scope = (cloud_env, user_id)
        now = monotonic()
        previous = self._last_sweep.get(scope)
        if previous is not None and now - previous < self.interval:
            return
        self._last_sweep[scope] = now
        try:
            for identity in self.journal.cache_cleanup_candidates(cloud_env, user_id):
                if not self.authorized(token, user_id):
                    break
                self.clean(token, identity)
        except Exception as error:
            logger.warning('publication cache sweep deferred: error_type=%s', type(error).__name__)
