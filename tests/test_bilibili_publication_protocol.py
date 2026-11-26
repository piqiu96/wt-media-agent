import unittest

from wt_media_agent.clients.bilibili.publication import (
    BilibiliPublicationError, accepted_receipt, find_submission,
    matching_partition, submission_id, work_url,
)


def reply(data):
    return {'status': 200, 'json': {'code': 0, 'data': data}}


class BilibiliProtocolTest(unittest.TestCase):
    def test_receipt_requires_one_forwarded_request_and_original_aid(self):
        valid = {**reply({'aid': 117590282275872, 'bvid': 'BV135WM6uE4A'}), 'count': 1, 'forwarded': 1}
        self.assertEqual(accepted_receipt(valid), {'platform_work_id': '117590282275872'})
        for change in ({'count': 2}, {'forwarded': 0}, {'status': 403},
                       {'json': {'code': 0, 'data': {'bvid': 'BV135WM6uE4A'}}},
                       {'json': {'code': -1, 'data': {'aid': 123}}},
                       {'json': {'code': False, 'data': {'aid': 123}}}):
            with self.subTest(change=change), self.assertRaises(BilibiliPublicationError):
                accepted_receipt({**valid, **change})

    def test_canonical_identity_never_guesses_from_bvid_or_url(self):
        self.assertEqual(work_url('123'), 'https://www.bilibili.com/video/av123')
        for bad in ('BV135WM6uE4A', '0123', '１２３', '+123', 1.5, True, None, '0'):
            with self.subTest(bad=bad), self.assertRaises(BilibiliPublicationError):
                submission_id(bad)

    def test_partition_matches_both_current_id_and_name(self):
        data = {'type_list': [{'id': 1008, 'name': '游戏'}]}
        matching_partition(data, 1008, '游戏')
        for number, name in ((17, '游戏'), (1008, '生活')):
            with self.assertRaises(BilibiliPublicationError):
                matching_partition(data, number, name)

    def test_finds_original_aid_across_real_pagination(self):
        calls = []
        def fetch(url):
            calls.append(url)
            page = len(calls)
            rows = range(1, 11) if page == 1 else [11]
            return reply({'page': {'pn': page, 'ps': 10, 'count': 11},
                          'arc_audits': [{'Archive': {'aid': i, 'title': 'same title'}} for i in rows]})
        self.assertEqual(find_submission(fetch, '11')['aid'], 11)
        self.assertEqual(len(calls), 2)

    def test_missing_original_is_not_latest_or_same_title(self):
        result = find_submission(lambda _: reply({'page': {'pn': 1, 'ps': 10, 'count': 1},
            'arc_audits': [{'Archive': {'aid': 999, 'title': 'same title'}}]}), '123')
        self.assertIsNone(result)

    def test_invalid_pagination_and_duplicates_fail_closed(self):
        for data in (
            {'page': {'pn': 1, 'ps': 10, 'count': 2}, 'arc_audits': []},
            {'page': {'pn': 1, 'ps': 10, 'count': 2}, 'arc_audits': [{'Archive': {'aid': 1}}] * 2},
            {'page': {'pn': 1, 'ps': 10, 'count': -1}, 'arc_audits': []},
        ):
            with self.subTest(data=data), self.assertRaises(BilibiliPublicationError):
                find_submission(lambda _: reply(data), '1')


if __name__ == '__main__':
    unittest.main()
