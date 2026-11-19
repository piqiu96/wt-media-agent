"""Local browser request guards using the process Profile admission barrier."""
from functools import wraps
from inspect import signature
from wt_media_agent.runtime.profile_operation import ProfileOperations, ProfileOperationBusy

def profile_operation(key, *, readonly_verify=False):
    """Keep a whole local request, including Cookie CDP work, in one lease."""
    def decorate(method):
        parameters = signature(method)
        @wraps(method)
        def guarded(self, *args, **kwargs):
            bound = parameters.bind(self, *args, **kwargs)
            body = bound.arguments["body"]
            profile_id=str(body.get(key, '')).strip()
            if not profile_id or (readonly_verify and kwargs.get('verify') is True):
                return method(self, *args, **kwargs)
            try:
                with self.profile_operations.acquire(profile_id):
                    return method(self, *args, **kwargs)
            except ProfileOperationBusy:
                return 409, {'error': {'code': 'profile_operation_busy'}}
        return guarded
    return decorate
