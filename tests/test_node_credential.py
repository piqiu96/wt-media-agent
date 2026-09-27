"""The credential Cloud issues for this node, and the two ends of its channel.

Cloud identifies a transfer call by this string and by nothing else: the frozen
`claim` request carries neither a path parameter nor a body, so there is no other
field in which "which node is asking" could travel. The Desktop obtains the
credential while registering the node and forwards it on the bind call this Agent
already serves; the Agent holds it in memory, and the transfer loop spends it.

Three things are asserted here, and each is a way the channel can be open at one
end while looking closed at the other:

* the holder -- one value, replaced by every bind, with no way to unset it;
* the bind route's *answer* -- read back from what is held rather than from the
  request, because an echo agrees just as well with a write that was dropped,
  and a caller has nothing else to check against;
* the wire -- what the Desktop posts is what the holder ends up holding, which
  is the half that `record_binding`'s own tests cannot see.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import UnusedBitBrowser

from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.runtime.node_credential import NodeCredential

CREDENTIAL = "node-credential-SENTINEL"
ROTATED = "node-credential-issued-by-the-next-bind"
NODE_ID = "node-7f3c"


class HolderTests(unittest.TestCase):
    """The value itself: one field, and the two things it must not do."""

    def test_a_fresh_holder_has_none(self) -> None:
        """`None` is not a failure, it is the state every Agent starts in.

        The loop reads this before Cloud has ever been spoken to, and it is what
        "nothing to claim" is decided from.
        """
        self.assertIsNone(NodeCredential().get())

    def test_a_bind_replaces_what_the_previous_one_held(self) -> None:
        """Re-binding is ordinary: the Desktop re-registers a node at will."""
        store = NodeCredential()
        store.set(CREDENTIAL)
        store.set(ROTATED)

        self.assertEqual(store.get(), ROTATED)

    def test_there_is_no_way_to_unbind_a_running_download_loop(self) -> None:
        """The user's ruling, made structural: the bind call is the only writer.

        A `clear` would be reachable from any local API call that carries no
        credential, and a running loop whose credential was dropped mid-download
        is a download that reports nothing and says nothing about why. The
        control first, because "no `clear`" is also true of an object with no
        methods at all.
        """
        store = NodeCredential()

        self.assertTrue(hasattr(store, "set"))
        self.assertTrue(hasattr(store, "get"))
        self.assertFalse(hasattr(store, "clear"))

    def test_the_repr_says_whether_it_is_set_and_not_what_it_is(self) -> None:
        """This value is redacted everywhere else; a repr is a place to forget.

        It ends up in tracebacks and log records, both of which go to disk and to
        the Desktop's log view.
        """
        store = NodeCredential()

        self.assertEqual(repr(store), "<NodeCredential unset>")
        store.set(CREDENTIAL)
        self.assertEqual(repr(store), "<NodeCredential set>")
        self.assertNotIn(CREDENTIAL, repr(store))


class BindRecordingTests(unittest.TestCase):
    """What the bind route records, and what its answer may say about it."""

    def api(self, *, store: NodeCredential | None) -> LocalApiServer:
        return LocalApiServer(
            LocalAgentState(),
            bitbrowser=UnusedBitBrowser(),
            node_credential=store,
        )

    def test_the_credential_the_bind_carries_reaches_the_holder(self) -> None:
        store = NodeCredential()

        status, _ = self.api(store=store).record_binding(
            {"node_id": NODE_ID, "node_credential": CREDENTIAL}
        )

        self.assertEqual(status, 200)
        self.assertEqual(store.get(), CREDENTIAL)

    def test_the_answer_says_whether_a_credential_is_held_and_never_which(self) -> None:
        """Read back from the holder, so a dropped write cannot look like a bind."""
        status, body = self.api(store=NodeCredential()).record_binding(
            {"node_id": NODE_ID, "node_credential": CREDENTIAL}
        )

        self.assertEqual(status, 200)
        self.assertIs(body["has_node_credential"], True)
        self.assertNotIn(CREDENTIAL, json.dumps(body))
        # The positive control for that `NotIn`: the body is a body, and the
        # field it is being searched for a *value* of is present.
        self.assertEqual(body["node_id"], NODE_ID)

    def test_a_bind_carrying_no_credential_changes_nothing_about_the_credential(self) -> None:
        """The local-only bind Desktop has always been able to make.

        It must stay possible -- and it must not be a way to unbind a loop that is
        already running, which is why the credential is left alone rather than
        cleared and why the answer still reports the one that is held.
        """
        store = NodeCredential()
        api = self.api(store=store)
        api.record_binding({"node_id": NODE_ID, "node_credential": CREDENTIAL})

        status, body = api.record_binding({"node_id": "another-node"})

        self.assertEqual(status, 200)
        self.assertEqual(store.get(), CREDENTIAL)
        self.assertIs(body["has_node_credential"], True)
        # And the rest of the call did its job: the node id is what the request
        # said, so "nothing happened" is not the reading.
        self.assertEqual(body["node_id"], "another-node")
        self.assertEqual(api.state.node_id, "another-node")

    def test_a_credential_arriving_where_there_is_nowhere_to_put_it_is_refused(self) -> None:
        """Answered, not dropped: the same rule the save-directory pair follows.

        A 200 here would tell the Desktop this Agent is bound to Cloud when the
        transfer loop has nothing to claim with, and the Desktop has no other way
        to find out -- the loop would simply never claim, in silence.
        """
        api = self.api(store=None)

        status, body = api.record_binding({"node_id": NODE_ID, "node_credential": CREDENTIAL})

        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": {"code": "node_credential_unavailable"}})
        # Refused, and not recorded on the way to being refused: a node id
        # recorded beside a credential that was dropped is half a binding.
        self.assertEqual(api.state.node_id, "")

    def test_a_process_with_nowhere_to_put_one_still_takes_a_local_bind(self) -> None:
        """The control for the refusal above: it is about the credential only.

        A bind that says nothing about Cloud is the local-only call, and it has
        to keep working on a server that was assembled without a holder --
        otherwise `/api/v1/bind` would be unusable in every mode that does not
        download.
        """
        api = self.api(store=None)

        status, body = api.record_binding({"node_id": NODE_ID})

        self.assertEqual(status, 200)
        self.assertEqual(body["node_id"], NODE_ID)
        self.assertIs(body["has_node_credential"], False)


class BindRouteTests(unittest.TestCase):
    """The same call, over a real socket: what the Desktop posts is what lands."""

    def setUp(self) -> None:
        self.store = NodeCredential()
        self.api = LocalApiServer(
            LocalAgentState(),
            bitbrowser=UnusedBitBrowser(),
            node_credential=self.store,
        )

    def _post(
        self, body: object, api: LocalApiServer | None = None
    ) -> tuple[int, dict[str, object], str]:
        """Post it, and hand back the raw text as well as the parsed body.

        The text is what the credential must not appear in: a value echoed inside
        a nested object would slip past an assertion written against the keys
        somebody remembered to check.
        """
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api or self.api))
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        try:
            data = json.dumps(body).encode("utf-8")
            request = urlrequest.Request(
                f"http://127.0.0.1:{httpd.server_port}/api/v1/bind",
                data=data,
                method="POST",
            )
            request.add_header("content-type", "application/json")
            try:
                # network-ok: the loopback HTTP server this call drives, started above
                with urlrequest.urlopen(request) as response:
                    text = response.read().decode("utf-8")
                    return response.status, json.loads(text), text
            except urlrequest.HTTPError as exc:
                text = exc.read().decode("utf-8")
                return exc.code, json.loads(text), text
        finally:
            httpd.shutdown()
            thread.join()
            httpd.server_close()

    def test_what_the_desktop_posts_is_what_the_transfer_loop_will_spend(self) -> None:
        status, body, text = self._post(
            {
                "node_id": NODE_ID,
                "binding_token": "cloud-runtime-bound",
                "node_credential": CREDENTIAL,
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(), CREDENTIAL)
        self.assertIs(body["has_node_credential"], True)
        self.assertEqual(body["node_id"], NODE_ID)
        self.assertNotIn(CREDENTIAL, text)

    def test_the_route_carries_the_value_it_was_given_and_not_one_it_remembers(self) -> None:
        """The route's payload reaches the API, measured by a second value.

        One post would also succeed against a route that passed a hard-coded
        string, and two different ones cannot.
        """
        self._post({"node_id": NODE_ID, "binding_token": "t", "node_credential": CREDENTIAL})
        self.assertEqual(self.store.get(), CREDENTIAL)

        status, _, _ = self._post(
            {"node_id": NODE_ID, "binding_token": "t", "node_credential": ROTATED}
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(), ROTATED)


    def test_a_refused_bind_is_an_http_status_and_not_a_200_with_a_note(self) -> None:
        """The 503 is what the Desktop branches on, so it has to survive the wire.

        The route and the API are two files, and an answer that was `(503, {...})`
        inside the API would reach the Desktop as `200` if the handler lost the
        status on the way out.
        """
        without_a_store = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())

        status, body, _ = self._post(
            {"node_id": NODE_ID, "binding_token": "t", "node_credential": CREDENTIAL},
            api=without_a_store,
        )

        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": {"code": "node_credential_unavailable"}})


if __name__ == "__main__":
    unittest.main()
