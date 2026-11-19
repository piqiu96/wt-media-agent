"""Serialize real Profile operations; business owners supply durable occupancy."""
from contextlib import contextmanager
from threading import Lock

class ProfileOperationBusy(RuntimeError):
    pass

class ProfileOperations:
    def __init__(self, *, occupied=lambda profile_id: False):
        self.occupied=occupied
        self._mutex=Lock()
        self._active=set()

    @contextmanager
    def registration(self, profile_id):
        # The same short admission barrier is used when a business owner saves
        # its durable claim. Never wait behind a running browser mutation.
        with self._mutex:
            if profile_id in self._active:
                raise ProfileOperationBusy('Profile operation is in progress')
            self._active.add(profile_id)
        try:
            yield
        finally:
            with self._mutex:
                self._active.remove(profile_id)

    @contextmanager
    def acquire(self, profile_id):
        with self.registration(profile_id):
            if self.occupied(profile_id):
                raise ProfileOperationBusy('Profile has an unresolved operation')
            yield

