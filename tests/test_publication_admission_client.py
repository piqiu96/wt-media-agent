"""The v2 publication consumer cannot confuse a recovered key with new work."""
import unittest

from wt_media_agent.clients.cloud.client import CloudAgentClient
from wt_media_agent.clients.cloud.contract import REQUIRED_CONTRACT_REVISION, is_cloud_agent_compatible
from wt_media_agent.clients.cloud.errors import CloudUnavailableError


class PublicationAdmissionClientTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.response = None
        def transport(method, path, body, headers):
            self.calls.append((method, path, body, headers))
            return {'errcode': 0, 'data': self.response}
        self.client = CloudAgentClient('https://cloud.example', transport=transport)
        self.capacity = {'automatic_slots': 5, 'window_slots': 10}

    def test_new_admission_advertises_both_configured_bounds(self):
        self.assertIsNone(self.client.claim_publication('token', 'original', capacity=self.capacity))
        self.assertEqual(self.calls[-1][2], {'request_key': 'original', 'capacity': self.capacity})
        self.assertIsNone(self.client.claim_publication_check('token', 'check-original', capacity=self.capacity))
        self.assertEqual(self.calls[-1][2], {'request_key': 'check-original', 'capacity': self.capacity})
        self.response = {'attempt_id': '2', 'task_status': 'running', 'stage': 'preparing', 'stop_requested': False, 'allow_submit': False, 'scene_parked': False}
        self.client.publication_human('token', '1', '2', '3', resume=True, capacity=self.capacity)
        self.assertEqual(self.calls[-1][2], {'attempt_id': '2', 'expected_revision': '3', 'capacity': self.capacity})

    def test_unreceived_settlement_keeps_original_key_and_never_requests_capacity(self):
        self.response = {'request_key': 'check-original', 'reservation_state': 'cancelled'}
        ack = self.client.settle_unreceived_publication_check('old-token', 'check-original')
        self.assertEqual(ack, self.response)
        self.assertEqual(self.calls[-1][0:3], ('POST', '/api/v1/cloud-agent/publication-task/check/claim',
            {'request_key': 'check-original', 'settle_unreceived': True}))

    def test_released_replay_has_no_executable_snapshot(self):
        self.response = {'publication_id': '1', 'attempt_id': '2', 'generation': '3', 'reservation_id': '4',
            'reservation_state': 'released', 'replayed': True, 'released': True}
        self.assertEqual(self.client.claim_publication_check('token', 'check-original', capacity=self.capacity), self.response)
        self.response = dict(self.response, task={'id': '1'})
        with self.assertRaises(CloudUnavailableError):
            self.client.claim_publication_check('token', 'check-original', capacity=self.capacity)

    def test_malformed_settlements_never_clear_the_original_intent(self):
        for data in [
            {'reservation_state': 'active', 'task': {'id': '1'}},
            {'reservation_state': 'cancelled', 'request_key': 'different'},
            {'reservation_state': 'released', 'replayed': True, 'released': True},
            {'publication_id': True, 'attempt_id': '2', 'generation': '3', 'reservation_id': '4',
             'reservation_state': 'released', 'replayed': True, 'released': True},
        ]:
            self.response = data
            with self.subTest(data=data), self.assertRaises(CloudUnavailableError):
                self.client.settle_unreceived_publication_check('old-token', 'check-original')

    def test_known_reservation_settlement_checks_the_exact_acknowledgement(self):
        packet = {'release_only': True, 'reservation_id': '4', 'attempt_id': '2', 'generation': '3', 'profile_disposition': 'closed'}
        self.response = {'publication_id': '1', 'attempt_id': '2', 'generation': '3', 'reservation_id': '5', 'released': True}
        with self.assertRaises(CloudUnavailableError):
            self.client.release_publication_check('token', '1', packet)
        self.response['reservation_id'] = '4'
        self.assertEqual(self.client.release_publication_check('token', '1', packet), self.response)

    def test_invalid_capacity_never_reaches_cloud(self):
        for capacity in [{'automatic_slots': True, 'window_slots': 10}, {'automatic_slots': 0, 'window_slots': 10},
                         {'automatic_slots': 5}, {'automatic_slots': 5, 'window_slots': 65536}]:
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                self.client.claim_publication('token', 'original', capacity=capacity)
        self.assertEqual(self.calls, [])

    def test_parallel_agent_refuses_legacy_publication_provider(self):
        self.assertEqual(REQUIRED_CONTRACT_REVISION, '2026.11.25.2')
        metadata = {'api': 'cloud-agent', 'major_version': 'v1', 'contract_revision': '2026.11.22.2',
            'minimum_agent_contract_revision': '2026.11.22.2', 'compatible_agent_major_versions': ['v1'], 'status': 'compatible'}
        self.assertFalse(is_cloud_agent_compatible(metadata))
