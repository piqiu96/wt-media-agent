"""Browser adapter safety checks; fixture responses are not platform acceptance."""
import tempfile,unittest
from pathlib import Path
from wt_media_agent.platforms.baidu.publisher import BrowserPublisher, PlatformFormError, publication_description, image_upload_file

class BrowserPublisherTests(unittest.TestCase):
    def test_confirmed_text_is_preserved_and_never_truncated(self):
        content={'description':'已核对的描述','topics':'#三角洲行动 #两端内容核对','declaration':'无需声明'}
        self.assertEqual(publication_description(content),'已核对的描述 #三角洲行动 #两端内容核对')
        with self.assertRaises(PlatformFormError):publication_description(dict(content,description='字'*51))
        with self.assertRaises(PlatformFormError):publication_description(dict(content,description=''))
    def test_cover_filename_uses_verified_image_bytes_without_changing_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cover';raw=b'\xff\xd8\xff\xe0verified-fixture-image';path.write_bytes(raw)
            upload=image_upload_file(path);self.assertEqual(upload.suffix,'.jpg');self.assertEqual(upload.read_bytes(),raw);self.assertEqual(path.read_bytes(),raw)
            self.assertEqual(upload.stat().st_mode&0o777,0o600)
            path.write_bytes(b'not an image')
            with self.assertRaises(PlatformFormError):image_upload_file(path)
    def test_submit_without_validated_form_never_clicks(self):
        adapter=BrowserPublisher()
        with self.assertRaises(PlatformFormError):adapter.submit()
    def test_receipt_without_this_instance_submit_is_rejected(self):
        adapter=BrowserPublisher()
        with self.assertRaises(PlatformFormError):adapter.read_receipt()

    def test_receipt_requires_one_matching_publish_response_and_preserves_large_id(self):
        from wt_media_agent.platforms.baidu.publisher import accepted_receipt
        import json
        capture={'count':1,'title':'确认内容','status':200,'text':json.dumps({'errno':0,'ret':{'article_id':1862233438702758904}})}
        self.assertEqual(accepted_receipt(capture,'确认内容'),{'platform_work_id':'1862233438702758904'})
        for patch in ({'count':2},{'title':'另一条内容'},{'status':500},{'text':'{}'},{'text':'{"errno":0,"data":{"mediaId":"123"}}'},{'text':'{"errno":1,"ret":{"article_id":"123"}}'},{'text':'{"errno":0,"ret":{"article_id":"0"}}'}):
            with self.subTest(patch=patch),self.assertRaises(PlatformFormError):accepted_receipt(dict(capture,**patch),'确认内容')
