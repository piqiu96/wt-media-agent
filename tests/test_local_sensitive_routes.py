"""本机绑定与敏感操作路由：事实由 Agent 实读，Cloud 只见到实读结果。"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.clients.bitbrowser import (
    BitBrowserIdentityError,
    BitBrowserResponseError,
    BitProfile,
    ProfileSnapshot,
)
from wt_media_agent.clients.cloud import AgentAuthError, CloudError, CloudUnavailableError
from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.runtime.agent_token import AgentToken

TOKEN = "v1.payload.signature"


class ScanClient:
    def __init__(self, *, main_user_id: str = "main-user-1", profile_ids: tuple[str, ...] = ("profile-1",)) -> None:
        self.main_user_id = main_user_id
        self.profile_ids = profile_ids

    def scan_profiles(self) -> ProfileSnapshot:
        return ProfileSnapshot(
            main_user_id=self.main_user_id,
            profiles=tuple(
                BitProfile(
                    bit_profile_id=profile_id,
                    profile_user_id="bit-user-1",
                    main_user_id=self.main_user_id,
                    name="运营窗口",
                    seq=1,
                    group_id="",
                    group_name="",
                    status=1,
                    bit_updated_at="",
                    remark="",
                    proxy_type="noproxy",
                    proxy_host="",
                    proxy_port=0,
                )
                for profile_id in self.profile_ids
            ),
        )


class FailingScanClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def scan_profiles(self) -> ProfileSnapshot:
        raise self.error


class RecordingCloud:
    """只实现 server 用到的三个方法，并记下每次调用。"""

    def __init__(self) -> None:
        self.bind_calls: list[dict[str, object]] = []
        self.preflight_calls: list[dict[str, object]] = []
        self.finish_calls: list[dict[str, object]] = []
        self.failure: Exception | None = None

    def bind_bit_account(self, token, *, main_user_id, observed_at, rebind=False):
        self._raise_if_failure()
        self.bind_calls.append(
            {
                "token": token,
                "main_user_id": main_user_id,
                "observed_at": observed_at,
                "rebind": rebind,
            }
        )
        return {"bound": True, "main_user_id": main_user_id}

    def preflight_sensitive_task(self, token, task_id, facts):
        self._raise_if_failure()
        self.preflight_calls.append({"token": token, "task_id": task_id, "facts": dict(facts)})
        return {"outcome": "granted", "permit_id": "permit-1", "profile_id": "profile-1"}

    def finish_sensitive_permit(self, token, permit_id, outcome):
        self._raise_if_failure()
        self.finish_calls.append({"token": token, "permit_id": permit_id, "outcome": outcome})

    def _raise_if_failure(self) -> None:
        if self.failure is not None:
            raise self.failure


def _server(*, cloud=None, scanner=None, token=TOKEN, with_cloud=True, with_store=True):
    """每次调用都新建 Cloud 与 Token 存储，免得一个用例的设定漏进下一个。"""
    agent_token = None
    if with_store:
        agent_token = AgentToken()
        if token is not None:
            agent_token.set(token, 1_800_000_000)
    client = cloud if cloud is not None else RecordingCloud()
    server = LocalApiServer(
        LocalAgentState(),
        bitbrowser=scanner or ScanClient(),
        agent_token=agent_token,
        cloud_agent=client if with_cloud else None,
    )
    return server, client


def _observed_at(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class RefusalBeforeCloudTests(unittest.TestCase):
    def test_a_server_without_a_cloud_client_says_so(self) -> None:
        server, _ = _server(with_cloud=False)

        status, payload = server.bit_account_bind_response({})

        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": {"code": "cloud_unavailable"}})

    def test_a_server_without_token_storage_says_so(self) -> None:
        server, _ = _server(with_store=False)

        status, payload = server.bit_account_bind_response({})

        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": {"code": "agent_token_unavailable"}})

    def test_an_unset_token_is_a_400_not_a_cloud_call(self) -> None:
        server, cloud = _server(token=None)

        status, payload = server.bit_account_bind_response({})

        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "agent_token_required"}})
        self.assertEqual(cloud.bind_calls, [])


class BitAccountBindTests(unittest.TestCase):
    def test_the_main_account_comes_from_a_live_scan_not_the_request(self) -> None:
        server, cloud = _server(scanner=ScanClient(main_user_id="main-read-from-bitbrowser"))

        status, payload = server.bit_account_bind_response(
            {"main_user_id": "typed-by-someone", "rebind": True}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["main_user_id"], "main-read-from-bitbrowser")
        call = cloud.bind_calls[0]
        self.assertEqual(call["main_user_id"], "main-read-from-bitbrowser")
        self.assertEqual(call["rebind"], True)
        self.assertEqual(call["token"], TOKEN)
        self.assertLess(
            abs((datetime.now(timezone.utc) - _observed_at(call["observed_at"])).total_seconds()),
            60,
        )

    def test_an_offline_bitbrowser_is_retryable(self) -> None:
        server, cloud = _server(
            scanner=FailingScanClient(BitBrowserResponseError("connection refused"))
        )

        status, payload = server.bit_account_bind_response({})

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "bit_browser_offline")
        self.assertTrue(payload["error"]["retryable"])
        self.assertEqual(payload["error"]["recovery_action"], "start_bitbrowser")
        self.assertEqual(cloud.bind_calls, [])

    def test_an_unverifiable_identity_is_not_reported_as_offline(self) -> None:
        server, cloud = _server(
            scanner=FailingScanClient(BitBrowserIdentityError("no single main account"))
        )

        status, payload = server.bit_account_bind_response({})

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "bitbrowser_identity_unverifiable")
        self.assertEqual(cloud.bind_calls, [])


class SensitivePreflightTests(unittest.TestCase):
    def test_the_facts_are_what_the_scan_saw(self) -> None:
        server, cloud = _server()

        status, payload = server.sensitive_preflight_response("task-1", {"profile_id": "profile-1"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["outcome"], "granted")
        facts = cloud.preflight_calls[0]["facts"]
        self.assertEqual(cloud.preflight_calls[0]["task_id"], "task-1")
        self.assertEqual(facts["bitbrowser_status"], "running")
        self.assertEqual(facts["main_user_id"], "main-user-1")
        self.assertEqual(facts["profile_visible"], True)
        self.assertLess(
            abs((datetime.now(timezone.utc) - _observed_at(facts["observed_at"])).total_seconds()),
            60,
        )

    def test_a_profile_missing_from_the_scan_is_not_visible(self) -> None:
        server, cloud = _server(scanner=ScanClient(profile_ids=("profile-2",)))

        _, _ = server.sensitive_preflight_response("task-1", {"profile_id": "profile-1"})

        self.assertEqual(cloud.preflight_calls[0]["facts"]["profile_visible"], False)

    def test_a_missing_profile_id_is_refused_locally(self) -> None:
        server, cloud = _server()

        status, payload = server.sensitive_preflight_response("task-1", {})

        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})
        self.assertEqual(cloud.preflight_calls, [])


class SensitivePermitFinishTests(unittest.TestCase):
    def test_a_completed_outcome_is_forwarded(self) -> None:
        server, cloud = _server()

        status, payload = server.sensitive_permit_finish_response(
            "permit-1", {"outcome": "completed"}
        )

        self.assertEqual(status, 204)
        self.assertEqual(payload, {})
        self.assertEqual(cloud.finish_calls, [{"token": TOKEN, "permit_id": "permit-1", "outcome": "completed"}])

    def test_an_unknown_outcome_is_refused_locally(self) -> None:
        server, cloud = _server()

        status, payload = server.sensitive_permit_finish_response(
            "permit-1", {"outcome": "maybe"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})
        self.assertEqual(cloud.finish_calls, [])


class CloudFailureMappingTests(unittest.TestCase):
    def test_an_auth_refusal_keeps_clouds_own_words(self) -> None:
        failure = AgentAuthError(
            "AUTH_DESKTOP_REPLACED",
            "你的账号已在另一台电脑登录",
            retryable=False,
            recovery_action="login_again",
        )
        server, cloud = _server()
        cloud.failure = failure

        status, payload = server.bit_account_bind_response({})

        self.assertEqual(status, 401)
        self.assertEqual(
            payload["error"],
            {
                "code": "AUTH_DESKTOP_REPLACED",
                "message": "你的账号已在另一台电脑登录",
                "retryable": False,
                "recovery_action": "login_again",
            },
        )

    def test_a_business_refusal_is_a_conflict(self) -> None:
        server, cloud = _server()
        cloud.failure = CloudError(
            "BIT_MAIN_USER_MISMATCH",
            "当前比特浏览器登录账号与系统绑定账号不一致",
            retryable=False,
            recovery_action="rebind_bitbrowser",
        )

        status, payload = server.bit_account_bind_response({})

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "BIT_MAIN_USER_MISMATCH")
        self.assertEqual(payload["error"]["recovery_action"], "rebind_bitbrowser")

    def test_an_unreachable_cloud_is_retryable(self) -> None:
        server, cloud = _server()
        cloud.failure = CloudUnavailableError("cloud could not be reached")

        status, payload = server.sensitive_preflight_response("task-1", {"profile_id": "profile-1"})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "cloud_unavailable")
        self.assertTrue(payload["error"]["retryable"])


class RoutingTests(unittest.TestCase):
    """路径形状是 Desktop 真正依赖的部分，所以走一次真实的 HTTP。"""

    def setUp(self) -> None:
        self.server, self.cloud = _server()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.server))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _post(self, path: str, body: dict[str, object]) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_port, timeout=10
        )
        try:
            payload = json.dumps(body).encode("utf-8")
            connection.request("POST", path, body=payload)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_bind_preflight_and_finish_reach_their_handlers(self) -> None:
        bind_status, _ = self._post("/api/v1/bit-account/bind", {"rebind": False})
        preflight_status, preflight_body = self._post(
            "/api/v1/sensitive-tasks/task-1/preflight", {"profile_id": "profile-1"}
        )
        finish_status, finish_body = self._post(
            "/api/v1/sensitive-permits/permit-1/finish", {"outcome": "result_uncertain"}
        )

        self.assertEqual(bind_status, 200)
        self.assertEqual(preflight_status, 200)
        self.assertEqual(json.loads(preflight_body)["data"]["permit_id"], "permit-1")
        self.assertEqual(finish_status, 204)
        self.assertEqual(finish_body, b"")
        self.assertEqual(self.cloud.preflight_calls[0]["task_id"], "task-1")
        self.assertEqual(self.cloud.finish_calls[0]["permit_id"], "permit-1")
        self.assertEqual(self.cloud.finish_calls[0]["outcome"], "result_uncertain")


if __name__ == "__main__":
    unittest.main()
