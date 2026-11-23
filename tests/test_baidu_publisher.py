"""Browser adapter safety checks; fixture responses are not platform acceptance."""
import tempfile,unittest
from pathlib import Path
from wt_media_agent.platforms.baidu.publisher import BrowserPublisher, image_upload_file
from wt_media_agent.platforms.baidu.publisher import PlatformFormError, publication_description

class BrowserPublisherTests(unittest.TestCase):
    def test_old_video_processing_cannot_confirm_the_current_upload(self):
        from unittest.mock import MagicMock
        adapter=BrowserPublisher();adapter.page=MagicMock()
        adapter.page.locator.return_value.evaluate_all.return_value=['old-cover']
        response=MagicMock();response.ok=True;response.request.method='POST'
        response.url='https://baijiahao.baidu.com/materialui/video/compuploadvideo'
        response.json.return_value={'error_code':0,'mediaId':'new-video'}
        adapter._remember_video_response(response)
        response.url='https://baijiahao.baidu.com/pcui/video/process'
        response.json.return_value={'errno':0,'data':{'status':'PUBLISHED','img_frame':['frame']}}
        response.request.post_data='mediaId=old-video'
        adapter._remember_video_response(response)
        self.assertFalse(adapter._video_processed)
        response.request.post_data='mediaId=new-video'
        response.json.return_value={'errno':0,'data':{'status':'RUNNING'}}
        adapter._remember_video_response(response)
        self.assertFalse(adapter._video_processed)
        response.json.return_value={'errno':0,'data':{'status':'PUBLISHED','img_frame':['frame']}}
        adapter._remember_video_response(response)
        self.assertTrue(adapter._video_processed)

    def test_missing_portrait_is_rejected_before_any_page_mutation(self):
        from unittest.mock import MagicMock
        from wt_media_agent.platforms.publish import PublishPayload
        page=MagicMock()
        payload=PublishPayload(Path('video.mp4'),Path('cover'),{},'uid')
        with self.assertRaises(PlatformFormError):
            BrowserPublisher().fill_fields(page,payload,stopped=lambda:False)
        self.assertEqual(page.mock_calls,[])
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
    def test_original_scene_blocks_manual_publish_before_authorized_click(self):
        from unittest.mock import Mock
        adapter=BrowserPublisher()
        page=Mock()
        adapter.protect_scene(page)
        adapter.protect_scene(page)
        self.assertEqual(page.route.call_count,1)
        guard=page.route.call_args.args[1]
        route=Mock()
        route.request.method='POST'
        route.request.url='https://baijiahao.baidu.com/pcui/article/publish?type=video'
        guard(route)
        route.abort.assert_called_once()
        route.fallback.assert_not_called()
        adapter._submitted=True
        guard(route)
        self.assertEqual(route.fallback.call_count,1)
        guard(route)
        self.assertEqual(route.abort.call_count,2)
        with self.assertRaises(PlatformFormError):adapter.protect_scene(Mock())

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

    def test_cover_wait_diagnostics_identify_stage_without_private_error_text(self):
        adapter=BrowserPublisher(timeout=0)
        adapter._check=lambda: None
        with self.assertLogs('wt_media_agent.platforms.baidu.publisher',level='INFO') as captured:
            with self.assertRaises(PlatformFormError):
                adapter._wait(lambda:False,stage='cover_preview_replaced',cover_index=0)
        self.assertEqual(len(captured.output),2)
        self.assertIn('stage=cover_preview_replaced cover_index=0 outcome=started',captured.output[0])
        self.assertIn('outcome=failed error_type=PlatformFormError',captured.output[1])
        def private_failure():
            raise ValueError('private url token and publication text')
        with self.assertLogs('wt_media_agent.platforms.baidu.publisher',level='INFO') as captured:
            with self.assertRaises(ValueError):
                adapter._wait(private_failure,stage='cover_upload_decode',cover_index=1)
        self.assertNotIn('private',str(captured.output))
        with self.assertLogs('wt_media_agent.platforms.baidu.publisher',level='INFO') as captured:
            self.assertEqual(adapter._wait(lambda:'ready',stage='cover_dialog_closed',cover_index=1),'ready')
        self.assertIn('outcome=completed',captured.output[-1])

    def test_failure_image_stays_in_private_attempt_folder(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            adapter=BrowserPublisher()
            adapter.page=Mock()
            adapter.page.screenshot.return_value=b'fixture screenshot'
            adapter._diagnostic_path=Path(folder)/'editor-failure.png'
            adapter._record_failure()
            self.assertEqual(adapter._diagnostic_path.read_bytes(),b'fixture screenshot')
            self.assertEqual(adapter._diagnostic_path.stat().st_mode & 0o777,0o600)
            adapter.page.screenshot.side_effect=RuntimeError('private URL')
            with self.assertLogs('wt_media_agent.platforms.baidu.publisher',level='INFO') as captured:
                adapter._record_failure()
            self.assertNotIn('private URL',str(captured.output))

    def test_navigation_failure_captures_private_image_and_preserves_error(self):
        from unittest.mock import Mock
        from wt_media_agent.platforms.publish import PublishPayload
        with tempfile.TemporaryDirectory() as folder:
            adapter=BrowserPublisher()
            page=Mock()
            error=RuntimeError('private navigation error')
            page.goto.side_effect=error
            page.screenshot.return_value=b'failed navigation image'
            payload=PublishPayload(Path(folder)/'video.mp4',Path(folder)/'cover',{},'uid')
            with self.assertRaises(RuntimeError) as captured:
                adapter.fill_form(page,payload,stopped=lambda:False)
            self.assertIs(captured.exception,error)
            self.assertEqual((Path(folder)/'editor-failure.png').read_bytes(),b'failed navigation image')
