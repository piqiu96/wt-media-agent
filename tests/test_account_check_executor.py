"""Tests for the account-check executor's browser navigation (CHG-056 T-05).

Two things are pinned here, both of which used to be invisible because nothing
covered this executor directly:

- the executor drives the browser through the client's *public* surface. It
  used to call `bitbrowser._post("/browser/open-url", ...)` -- a private method
  of another layer -- with the URL spelled out inside `executors/`.
- the URLs themselves come from `clients/` (ADR-0016 §2/§3). The fake client
  below has no `_post`, so a call that reached for one fails here instead of
  quietly working.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.clients import platform_urls
from wt_media_agent.clients.bitbrowser import PROXY_PROBE_URL
from wt_media_agent.executors.account_check import AccountCheckExecutor


class FakeCloud:
    def __init__(self):
        self.reports = []

    def report_task(self, task_id, agent_id, status, progress, message="", data=None):
        self.reports.append((task_id, status, progress, message))
        return {"status": status, "progress": progress}


class FakeBitBrowser:
    """Public-surface stand-in: it answers `open_url` and nothing private."""

    def __init__(self, cookies, fail_on: str | None = None):
        self.cookies = cookies
        self.fail_on = fail_on
        self.opened_urls: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def open_profile(self, profile_id: str) -> None:
        pass

    def open_url(self, profile_id: str, url: str) -> None:
        if self.fail_on is not None and url == self.fail_on:
            raise RuntimeError("navigation refused")
        self.opened_urls.append((profile_id, url))

    def read_cookies(self, profile_id: str):
        return self.cookies

    def close_profile(self, profile_id: str) -> None:
        self.closed.append(profile_id)


def build(platform="bilibili", cookies=(("DedeUserID", "293793435"),), **kwargs):
    client = FakeCloud()
    bitbrowser = FakeBitBrowser([{"name": n, "value": v} for n, v in cookies], **kwargs)
    return client, bitbrowser, AccountCheckExecutor(client, "agent-1", bitbrowser)


class AccountCheckNavigationTests(unittest.TestCase):
    def test_the_platform_login_url_comes_from_the_clients_layer(self):
        client, bitbrowser, executor = build(platform="bilibili")

        executor.execute({"task_id": "task-1", "profile_id": "profile-1", "platform": "bilibili"})

        self.assertEqual(
            [url for _, url in bitbrowser.opened_urls],
            [platform_urls.LOGIN_URLS["bilibili"], PROXY_PROBE_URL],
        )

    def test_the_navigation_target_is_the_one_the_clients_layer_declares(self):
        """Paired control: the URL follows the table rather than a copy of it."""
        client, bitbrowser, executor = build(platform="baijiahao")

        executor.execute({"task_id": "task-1", "profile_id": "p1", "platform": "baijiahao"})

        self.assertEqual(bitbrowser.opened_urls[0][1], platform_urls.LOGIN_URLS["baijiahao"])

    def test_an_unsupported_platform_is_rejected_before_anything_is_reported(self):
        """A caller error must not be turned into a `check_failed` result.

        The executor catches `Exception` around its work; the platform lookup
        therefore has to happen outside that block, or an unsupported platform
        would be reported to Cloud as a failed check with the message as data.
        """
        client, bitbrowser, executor = build()

        with self.assertRaises(ValueError) as caught:
            executor.execute({"task_id": "task-1", "profile_id": "p1", "platform": "weibo"})

        self.assertEqual(str(caught.exception), "unsupported platform: weibo")
        self.assertEqual(client.reports, [])
        self.assertEqual(bitbrowser.opened_urls, [])

    def test_a_failed_proxy_probe_does_not_fail_the_check(self):
        """The probe is best-effort; only the navigation to the platform matters."""
        client, bitbrowser, executor = build(fail_on=PROXY_PROBE_URL)

        result = executor.execute({"task_id": "task-1", "profile_id": "p1", "platform": "bilibili"})

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([url for _, url in bitbrowser.opened_urls], [platform_urls.LOGIN_URLS["bilibili"]])

    def test_no_cookies_means_not_logged_in_and_no_probe(self):
        client, bitbrowser, executor = build(cookies=())

        executor.execute({"task_id": "task-1", "profile_id": "p1", "platform": "bilibili"})

        self.assertEqual([url for _, url in bitbrowser.opened_urls], [platform_urls.LOGIN_URLS["bilibili"]])
        self.assertEqual(bitbrowser.closed, ["p1"])


class PlatformTableTests(unittest.TestCase):
    """The table's own contract, kept apart from the tests that read it.

    The navigation tests above compare against `LOGIN_URLS`, so they stay green
    under any edit to it -- including a typo'd host, which would send profiles
    to the wrong site. The platform ids are a compatibility surface instead:
    Cloud tasks name them, and `login_url` rejects anything else.
    """

    def test_the_supported_platform_ids_are_the_ones_tasks_carry(self):
        self.assertEqual(set(platform_urls.LOGIN_URLS), {"douyin", "bilibili", "baijiahao"})

    def test_every_entry_points_at_the_https_host_it_names(self):
        expected = {
            "douyin": "www.douyin.com",
            "bilibili": "www.bilibili.com",
            "baijiahao": "baijiahao.baidu.com",
        }
        for platform, host in expected.items():
            with self.subTest(platform=platform):
                url = platform_urls.LOGIN_URLS[platform]
                self.assertEqual(urlsplit(url).scheme, "https")
                self.assertEqual(urlsplit(url).netloc, host)

    def test_an_unknown_platform_raises_rather_than_returning_a_default(self):
        with self.assertRaises(ValueError) as caught:
            platform_urls.login_url("weibo")
        self.assertEqual(str(caught.exception), "unsupported platform: weibo")


if __name__ == "__main__":
    unittest.main()
