import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication_draft import PublicationDraftStore


class DraftAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'agent.sqlite3')
        apply_migrations(self.db)
        self.api = LocalApiServer(LocalAgentState(), bitbrowser=object(), auth_token='runtime-secret', publication_draft=PublicationDraftStore(self.db, environment='dev'))
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.api))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()
        self.db.close()
        self.temp.cleanup()

    def request(self, method='GET', body=None, user='9', token='runtime-secret', kind='task'):
        headers = {'Authorization': 'Bearer ' + token, 'X-Publication-User-ID': user, 'Content-Type': 'application/json'}
        connection = http.client.HTTPConnection(*self.http.server_address)
        connection.request(method, '/api/v1/publication-draft/' + kind, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_http_persistence_conflict_clear_and_other_user(self):
        self.assertEqual(self.request()[1]['data']['revision'], '0')
        self.assertEqual(self.request('PUT', {'payload': {'description': '稿'}, 'expected_revision': '0'})[0], 200)
        self.assertEqual(self.request(user='10')[1]['data']['payload'], None)
        status, body = self.request('PUT', {'payload': {'description': '旧'}, 'expected_revision': '0'})
        self.assertEqual((status, body['error']['code']), (409, 'draft_revision_conflict'))
        self.assertEqual(self.request('DELETE', {'expected_revision': '1'})[1]['data']['revision'], '2')
        self.assertEqual(self.request('PUT', {'payload': {'description': '旧'}, 'expected_revision': '1'})[0], 409)

    def test_batch_http_type_isolated_and_authenticated(self):
        status, body = self.request('PUT', {'payload': {'request_key': 'batch-key'}, 'expected_revision': '0'}, kind='batch')
        self.assertEqual(status, 200)
        self.assertEqual(body['data']['revision'], '1')
        self.assertIsNone(self.request()[1]['data']['payload'])
        self.assertEqual(self.request(kind='batch', token='wrong')[0], 401)
        self.assertEqual(self.request(kind='unknown')[0], 400)
        self.assertEqual(self.request('DELETE', {'expected_revision': '1'}, kind='batch')[1]['data']['revision'], '2')
        self.assertEqual(self.request('PUT', {'payload': {}, 'expected_revision': '1'}, kind='batch')[0], 409)

    def test_identity_and_runtime_auth_are_required(self):
        self.assertEqual(self.request(token='wrong')[0], 401)
        self.assertEqual(self.request(user='')[0], 403)
        self.api.auth_token = ''
        self.assertEqual(self.request()[0], 401)

    def test_invalid_input_never_changes_storage(self):
        self.assertEqual(self.request('PUT', {'payload': [], 'expected_revision': '0'})[0], 400)
        self.assertEqual(self.request('PUT', {'payload': {}, 'expected_revision': 0})[0], 400)
        connection = http.client.HTTPConnection(*self.http.server_address)
        connection.putrequest('PUT', '/api/v1/publication-draft/task')
        connection.putheader('Authorization', 'Bearer runtime-secret')
        connection.putheader('X-Publication-User-ID', '9')
        connection.putheader('Content-Length', str(3 * 1024 * 1024))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()
        self.assertIsNone(self.request()[1]['data']['payload'])

    def test_plan_http_cas_and_identity_follow_original_draft_contract(self):
        payload = {'request_key': 'plan-original', 'goals': [], 'batches': []}
        status, body = self.request('PUT', {'payload': payload, 'expected_revision': '0'}, kind='plan')
        self.assertEqual(status, 200)
        self.assertEqual(body['data']['revision'], '1')
        self.assertEqual(self.request(kind='plan')[1]['data']['payload'], payload)
        self.assertIsNone(self.request(kind='plan', user='10')[1]['data']['payload'])
        self.assertEqual(self.request(kind='plan', token='wrong')[0], 401)
        self.assertEqual(self.request(kind='plan', user='')[0], 403)
        self.assertEqual(self.request('DELETE', {'expected_revision': '1'}, kind='plan')[0], 200)
        self.assertEqual(self.request('PUT', {'payload': payload, 'expected_revision': '1'}, kind='plan')[0], 409)
        self.assertIsNone(self.request(kind='batch')[1]['data']['payload'])
