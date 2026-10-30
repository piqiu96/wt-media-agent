from __future__ import annotations

from pathlib import Path
from http.server import ThreadingHTTPServer
import json
import sys
import threading
import unittest
from unittest.mock import patch
from urllib import request as urlrequest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.clients.bitbrowser import BitProfile, ProfileSnapshot


class MutationClient:
    def __init__(self, apply_updates: bool = False, clear_proxy_on_unbind: bool = True) -> None:
        self.updated: list[tuple[str, dict[str, object]]] = []
        self.apply_updates = apply_updates
        self.clear_proxy_on_unbind = clear_proxy_on_unbind
        self.proxy_type = "socks5"
        self.proxy_host = "127.0.0.1"
        self.proxy_port = 1080

    def update_profile(self, profile_id: str, config: dict[str, object]) -> None:
        self.updated.append((profile_id, config))
        if not self.apply_updates:
            return
        self.proxy_type = str(config.get("proxyType", ""))
        if self.proxy_type == "noproxy" and not self.clear_proxy_on_unbind:
            return
        self.proxy_host = str(config.get("host", ""))
        self.proxy_port = int(config.get("port", 0) or 0)

    def scan_profiles(self) -> ProfileSnapshot:
        return ProfileSnapshot("main-1", (BitProfile(
            bit_profile_id="bit-profile-1", profile_user_id="user-1", main_user_id="main-1",
            name="窗口", seq=1, group_id="group-1", group_name="分组", status=1,
            bit_updated_at="2026-09-04T00:00:00Z", remark="", proxy_type=self.proxy_type,
            proxy_host=self.proxy_host, proxy_port=self.proxy_port,
        ),))


class ProxyMutationTests(unittest.TestCase):
    def test_mutation_writes_then_returns_secret_free_readback(self) -> None:
        client = MutationClient()
        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=client).proxy_mutation_response({
            "profile_id": "bit-profile-1", "proxy_protocol": "socks5", "host": "127.0.0.1", "port": 1080,
            "username": "secret-user", "password": "secret-password",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": {"profile_id": "bit-profile-1", "proxy_protocol": "socks5", "host": "127.0.0.1", "port": 1080, "readback": True}})
        self.assertEqual(client.updated, [("bit-profile-1", {"proxyType": "socks5", "host": "127.0.0.1", "port": 1080, "proxyUserName": "secret-user", "proxyPassword": "secret-password"})])
        self.assertNotIn("password", str(payload).lower())

    def test_mutation_does_not_report_success_when_readback_differs(self) -> None:
        client = MutationClient()
        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=client).proxy_mutation_response({
            "profile_id": "bit-profile-1", "proxy_protocol": "http", "host": "127.0.0.1", "port": 1080,
        })

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "proxy_mutation_readback_mismatch")

    def test_unbind_writes_no_proxy_and_requires_no_proxy_readback(self) -> None:
        client = MutationClient(apply_updates=True)

        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=client).proxy_mutation_response({
            "operation": "unbind", "profile_id": "bit-profile-1",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": {"operation": "unbind", "profile_id": "bit-profile-1", "readback": True}})
        self.assertEqual(client.updated, [("bit-profile-1", {
            "proxyType": "noproxy", "proxyMethod": 2,
            "host": "", "port": 0,
            "proxyUserName": "", "proxyPassword": "",
        })])

    def test_unbind_rejects_readback_with_residual_proxy_address(self) -> None:
        client = MutationClient(apply_updates=True, clear_proxy_on_unbind=False)

        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=client).proxy_mutation_response({
            "operation": "unbind", "profile_id": "bit-profile-1",
        })

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "proxy_mutation_readback_mismatch")

class ProxyAssignmentAckTests(unittest.TestCase):
    def test_explicit_ack_succeeds_without_post_write_scan(self) -> None:
        client = MutationClient()
        def forbidden_scan():
            raise AssertionError("normal assignment must not scan after writing")
        client.scan_profiles = forbidden_scan
        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=client).proxy_assignment_response({
            "profile_id": "bit-profile-1", "proxy_protocol": "socks5", "host": "127.0.0.1", "port": 1080,
            "username": "secret-user", "password": "secret-password",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": {"profile_id": "bit-profile-1", "accepted": True}})
        self.assertNotIn("secret", str(payload))

    def test_explicit_rejection_and_unknown_outcome_are_distinct(self) -> None:
        from wt_media_agent.clients.bitbrowser.errors import BitBrowserRejectedError, BitBrowserUnavailableError
        for error, status, code in [(BitBrowserRejectedError("no"), 409, "proxy_assignment_rejected"),
                                    (BitBrowserUnavailableError("timeout"), 503, "proxy_assignment_unconfirmed")]:
            with self.subTest(status=status):
                client = MutationClient()
                def fail(*args): raise error
                client.update_profile = fail
                actual, payload = LocalApiServer(LocalAgentState(), bitbrowser=client).proxy_assignment_response({
                    "profile_id": "bit-profile-1", "proxy_protocol": "socks5", "host": "127.0.0.1", "port": 1080,
                })
                self.assertEqual(actual, status)
                self.assertEqual(payload["error"]["code"], code)

class ProxyAssignmentAdapterTests(unittest.TestCase):
    def test_metadata_edit_preserves_existing_proxy_fields(self) -> None:
        from wt_media_agent.clients.bitbrowser import BitBrowserClient
        sent = []
        def transport(url, payload, timeout):
            if url.endswith('/browser/detail'):
                return {'success': True, 'data': {
                    'browserFingerPrint': {'id': 'existing'}, 'proxyMethod': 2,
                    'proxyType': 'socks5', 'host': '127.0.0.1', 'port': 1080,
                    'proxyUserName': 'kept-user', 'proxyPassword': 'kept-password',
                }}
            sent.append(payload)
            return {'success': True, 'data': {}}
        BitBrowserClient('http://127.0.0.1:54345', transport=transport).update_profile(
            'p1', {'name': 'updated-name', 'remark': 'updated-note'})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['proxyType'], 'socks5')
        self.assertEqual(sent[0]['host'], '127.0.0.1')
        self.assertEqual(sent[0]['port'], 1080)
        self.assertEqual(sent[0]['proxyUserName'], 'kept-user')
        self.assertEqual(sent[0]['proxyPassword'], 'kept-password')

    def test_bitbrowser_ack_uses_prewrite_identity_read_but_no_postwrite_scan(self) -> None:
        from wt_media_agent.clients.bitbrowser import BitBrowserClient
        calls = []
        def transport(url, payload, timeout):
            calls.append(url.rsplit('/', 2)[-1])
            if url.endswith('/browser/detail'):
                return {'success': True, 'data': {'browserFingerPrint': {'id':'existing'}, 'proxyMethod':2}}
            if url.endswith('/browser/update'):
                return {'success': True, 'data': {}}
            raise AssertionError('unexpected post-write read')
        api = LocalApiServer(LocalAgentState(), bitbrowser=BitBrowserClient('http://127.0.0.1:54345', transport=transport))
        status, result = api.proxy_assignment_response({'profile_id':'p1','proxy_protocol':'socks5','host':'127.0.0.1','port':1080})
        self.assertEqual(status, 200)
        self.assertTrue(result['data']['accepted'])
        self.assertEqual(calls, ['detail', 'update'])

    def test_explicit_bitbrowser_rejection_does_not_echo_sensitive_message(self) -> None:
        from wt_media_agent.clients.bitbrowser import BitBrowserClient
        def transport(url, payload, timeout):
            if url.endswith('/browser/detail'):
                return {'success': True, 'data': {'browserFingerPrint': {}, 'proxyMethod': 2}}
            return {'success': False, 'msg': 'private-provider-error'}
        api = LocalApiServer(LocalAgentState(), bitbrowser=BitBrowserClient('http://127.0.0.1:54345', transport=transport))
        status, result = api.proxy_assignment_response({'profile_id':'p1','proxy_protocol':'socks5','host':'127.0.0.1','port':1080})
        self.assertEqual(status, 409)
        self.assertEqual(result['error']['code'], 'proxy_assignment_rejected')
        self.assertNotIn('private-provider-error', str(result))

    def test_missing_success_flag_is_not_treated_as_rejection_or_success(self) -> None:
        from wt_media_agent.clients.bitbrowser import BitBrowserClient
        def transport(url, payload, timeout):
            if url.endswith('/browser/detail'):
                return {'success': True, 'data': {'browserFingerPrint': {}, 'proxyMethod': 2}}
            return {'data': {}}
        api = LocalApiServer(LocalAgentState(), bitbrowser=BitBrowserClient('http://127.0.0.1:54345', transport=transport))
        status, result = api.proxy_assignment_response({'profile_id':'p1','proxy_protocol':'socks5','host':'127.0.0.1','port':1080})
        self.assertEqual(status, 503)
        self.assertEqual(result['error']['code'], 'proxy_assignment_unconfirmed')

if __name__ == "__main__":
    unittest.main()

class ProxyRecoveryTests(unittest.TestCase):
    def test_normal_apply_and_unbind_use_ack_without_scan(self):
        for operation in ("assign", "unbind"):
            client = MutationClient()
            client.scan_profiles = lambda: self.fail("normal writes must not scan")
            status, payload = LocalApiServer(LocalAgentState(), bitbrowser=client).proxy_write_response({
                "operation": operation, "profile_id": "p1", "proxy_protocol": "http", "host": "127.0.0.1", "port": 8080,
                "username": "private-user", "password": "private-password"})
            self.assertEqual((status, payload), (200, {"data": {"accepted": True}}))

    def test_local_compare_includes_auth_and_never_returns_secrets(self):
        from wt_media_agent.clients.bitbrowser import BitBrowserClient
        expected = {"proxyType": "http", "host": "127.0.0.1", "port": 8080, "proxyUserName": "user", "proxyPassword": "secret"}
        for mutation, outcome in (({}, "matches"), ({"proxyPassword": "other"}, "different"), ({"proxyPassword": None}, "unverifiable"), ({"proxyPassword": "******"}, "unverifiable"), ({"proxyType": "noproxy", "proxyPassword": None}, "different"), ({"host": "different-host", "proxyPassword": None}, "different")):
            data = {**expected, **mutation}
            calls = []
            def transport(url, payload, timeout):
                calls.append(url)
                return {"success": True, "data": data}
            api = LocalApiServer(LocalAgentState(), bitbrowser=BitBrowserClient("http://local", transport=transport))
            status, result = api.proxy_write_response({"profile_id": "p1", "operation": "assign", "proxy_protocol": "http", "host": "127.0.0.1", "port": 8080, "username": "user", "password": "secret"}, verify=True)
            self.assertEqual((status, result), (200, {"data": {"comparison": outcome}}))
            self.assertEqual(calls, ["http://local/browser/detail"])
            self.assertNotIn("secret", str(result))

    def test_loopback_write_and_verification_send_http_receipts(self):
        api = LocalApiServer(LocalAgentState(), bitbrowser=MutationClient())
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for path, verify, expected in (
                ("proxy-write", False, {"data": {"accepted": True}}),
                ("proxy-verification", True, {"data": {"comparison": "matches"}}),
            ):
                with self.subTest(path=path), patch.object(api, "proxy_write_response", return_value=(200, expected)) as action:
                    req = urlrequest.Request(
                        f"http://127.0.0.1:{server.server_port}/api/v1/{path}",
                        data=json.dumps({"profile_id": "p1"}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    # network-ok: connects only to the temporary loopback server above
                    with urlrequest.urlopen(req, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response), expected)
                    action.assert_called_once_with({"profile_id": "p1"}, **({"verify": True} if verify else {}))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
