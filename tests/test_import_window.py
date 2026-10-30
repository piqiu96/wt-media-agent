from types import SimpleNamespace
from unittest.mock import Mock
from wt_media_agent.services.browser.import_window import prepare_window as _prepare_window
from wt_media_agent.clients.bitbrowser.errors import BitBrowserRejectedError

def prepare_window(b,body):return _prepare_window(b,body,lambda e:isinstance(e,BitBrowserRejectedError))

BODY={'creation_marker':'WT_IMPORT:12:3:1','name':'test','groupId':'g'}
def snap(*profiles):return SimpleNamespace(profiles=profiles)
def profile(marker='WT_IMPORT:12:3:1'):return SimpleNamespace(remark=marker,bit_profile_id='bit')
import unittest

class ImportWindowTests(unittest.TestCase):
    def test_marker_recovery_never_creates(self):
        b=Mock();b.scan_profiles.return_value=snap(profile())
        _,r=prepare_window(b,{**BODY,'inspect_only':True})
        assert r['data']=={'id':'bit','status':'existing'}
        b.create_profile.assert_not_called()
    def test_missing_recovery_does_not_create(self):
        b=Mock();b.scan_profiles.return_value=snap()
        assert prepare_window(b,{**BODY,'inspect_only':True})[1]['data']['status']=='uncertain'
        b.create_profile.assert_not_called()
    def test_timeout_does_not_claim_unavailable(self):
        b=Mock();b.scan_profiles.return_value=snap();b.create_profile.side_effect=TimeoutError()
        assert prepare_window(b,BODY)[1]['data']['status']=='uncertain'
        b.create_profile.assert_called_once()
    def test_explicit_create_rejection_allows_fallback(self):
        b=Mock();b.scan_profiles.return_value=snap();b.create_profile.side_effect=BitBrowserRejectedError('capacity')
        assert prepare_window(b,BODY)[1]['data']['status']=='unavailable'
    def test_scan_failure_does_not_allow_fallback(self):
        b=Mock();b.scan_profiles.side_effect=BitBrowserRejectedError('scan')
        assert prepare_window(b,BODY)[1]['data']['status']=='uncertain'
        b.create_profile.assert_not_called()
    def test_create_must_read_back_exact_marker(self):
        b=Mock();b.scan_profiles.side_effect=[snap(),snap(profile('WT_IMPORT:12:3:10'))];b.create_profile.return_value='bit'
        assert prepare_window(b,BODY)[1]['data']['status']=='uncertain'
    def test_create_persists_marker_and_removes_control_fields(self):
        b=Mock();b.scan_profiles.side_effect=[snap(),snap(profile())];b.create_profile.return_value='bit'
        assert prepare_window(b,BODY)[1]['data']['status']=='created'
        body=b.create_profile.call_args.args[0]
        assert body['remark']==BODY['creation_marker'] and 'creation_marker' not in body
