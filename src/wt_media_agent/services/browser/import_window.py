"""One marked window creation, or read-only recovery; no business queue."""
import re
import threading

_lock = threading.Lock()

def prepare_window(bitbrowser, body, is_rejected):
    marker = str(body.get('creation_marker', ''))
    if not re.fullmatch(r'WT_IMPORT:[1-9][0-9]*:[1-9][0-9]*:[1-9][0-9]*', marker):
        return 400, {'error': {'code': 'invalid_request', 'message': '窗口创建标识无效'}}
    # A grant is never replayed by Cloud. This also serializes requests in this
    # process; after an uncertain response every recovery is inspect_only.
    with _lock:
        try:
            matches = [p for p in bitbrowser.scan_profiles().profiles if marker in p.remark.splitlines()]
            if len(matches) == 1:
                return 201, {'data': {'id': matches[0].bit_profile_id, 'status': 'existing'}}
            if matches or body.get('inspect_only'):
                return 201, {'data': {'id': '', 'status': 'uncertain'}}
            config = {k: v for k, v in body.items() if k not in ('creation_marker', 'inspect_only')}
            config['remark'] = marker
            try:
                profile_id = bitbrowser.create_profile(config)
            except Exception as error:
                if is_rejected(error):
                    return 201, {'data': {'id': '', 'status': 'unavailable'}}
                raise
            found = [p for p in bitbrowser.scan_profiles().profiles if p.bit_profile_id == profile_id and marker in p.remark.splitlines()]
            if len(found) == 1:
                return 201, {'data': {'id': profile_id, 'status': 'created'}}
        except Exception:
            # No blind retry even when the API created a window before timing out.
            pass
        return 201, {'data': {'id': '', 'status': 'uncertain'}}
