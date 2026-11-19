"""Local browser request guards using the process Profile admission barrier."""
from functools import wraps
from wt_media_agent.runtime.profile_operation import ProfileOperations, ProfileOperationBusy

def profile_operation(key, *, readonly_verify=False):
    """Keep a whole local request, including Cookie CDP work, in one lease."""
    def decorate(method):
        @wraps(method)
        def guarded(self, body, *args, **kwargs):
            profile_id=str(body.get(key, '')).strip()
            if not profile_id or (readonly_verify and kwargs.get('verify') is True):
                return method(self, body, *args, **kwargs)
            try:
                with self.profile_operations.acquire(profile_id):
                    return method(self, body, *args, **kwargs)
            except ProfileOperationBusy:
                return 409, {'error': {'code': 'profile_operation_busy'}}
        return guarded
    return decorate
