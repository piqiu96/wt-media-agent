import unittest

from wt_media_agent.adapters.douyin import DouyinAdapter, DouyinApiError


class DouyinAdapterTests(unittest.TestCase):
    def test_fetch_normalizes_reference_response_without_credentials_in_payload(self):
        calls = []

        def transport(path, fields, query):
            calls.append((path, fields, query))
            return {"result": 1, "data": {"aweme_id": "123", "desc": "标题 #热点", "author": {"uid": "u1", "nickname": "作者"}, "create_time": 1700000000}}

        item = DouyinAdapter(api_key="secret", transport=transport).fetch_by_url("https://v.douyin.com/demo")
        self.assertEqual(item["platform_content_id"], "123")
        self.assertEqual(item["author_id"], "u1")
        self.assertEqual(item["tags"], "热点")
        self.assertNotIn("secret", str(calls[0][1]))
        self.assertEqual(calls[0][0], "/dyVideo/detail")

    def test_missing_credentials_fails_closed(self):
        with self.assertRaises(DouyinApiError):
            DouyinAdapter(api_key="").search("王者荣耀")

    def test_search_maps_nested_aweme_items(self):
        def transport(path, fields, query):
            return {"result": 1, "data": {"data": [{"aweme_info": {"aweme_id": "1", "desc": "one"}}, {"aweme_info": {"aweme_id": "2", "desc": "two"}}]}}

        result = DouyinAdapter(api_key="secret", transport=transport).search("demo", limit=2)
        self.assertEqual([item["platform_content_id"] for item in result], ["1", "2"])
