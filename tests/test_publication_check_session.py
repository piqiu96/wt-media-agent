"""Controlled read resources never reuse absence evidence after an open attempt."""
import unittest
from unittest.mock import Mock
from wt_media_agent.services.browser import session


class ProfileReadSessionTests(unittest.TestCase):
    def resources(self, bit):
        self.assertTrue(hasattr(session, 'ProfileReadSession'), 'missing controlled Profile read lifecycle')
        return session.ProfileReadSession(bit, 'profile')

    def test_failed_open_invalidates_absence_without_closing_unknown_scene(self):
        bit = Mock()
        bit.is_profile_closed.return_value = True
        bit.open_profile_for_check.side_effect = TimeoutError('lost open response')
        resources = self.resources(bit)
        with self.assertRaises(TimeoutError), resources:
            self.assertTrue(resources.closed)
            with resources.browser():
                self.fail('open must fail')
        self.assertFalse(resources.closed)
        self.assertFalse(resources.browser_used)
        bit.close_profile_verified.assert_not_called()

    def test_failed_close_invalidates_absence_and_records_browser_use(self):
        bit = Mock()
        bit.is_profile_closed.return_value = True
        bit.open_profile_for_check.return_value = '127.0.0.1:1234'
        bit.close_profile_verified.side_effect = TimeoutError('lost close response')
        resources = self.resources(bit)
        with self.assertRaises(TimeoutError), resources:
            with resources.browser() as endpoint:
                self.assertEqual(endpoint, '127.0.0.1:1234')
                self.assertFalse(resources.closed)
        self.assertFalse(resources.closed)
        self.assertTrue(resources.browser_used)
        bit.close_profile_verified.assert_called_once_with('profile', '127.0.0.1:1234')

    def test_http_only_read_rechecks_profile_absence(self):
        bit = Mock()
        bit.is_profile_closed.side_effect = [True, False]
        resources = self.resources(bit)
        with resources:
            resources.read_environment()
        self.assertFalse(resources.closed)
        bit.read_request_environment.assert_called_once_with('profile')
        bit.open_profile_for_check.assert_not_called()
