"""One owned Bilibili editor, frozen fields, one authorized final POST."""
import hashlib
import json
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from wt_media_agent.clients.bilibili.cover import cover_upload_kind

from wt_media_agent.clients.bilibili.publication import (
    ADD_PATH, CREATOR_ORIGIN, PRE_PATH, TYPE_PATH, BilibiliPublicationError,
    accepted_receipt, matching_partition, response_data, submission_id,
)
from wt_media_agent.platforms.bilibili.identity import read_bilibili_identity
from wt_media_agent.platforms.publish import PublicationInputError
from wt_media_agent.runtime.constants import PublishMethod
from wt_media_agent.services.browser.session import BrowserSession


class PlatformFormError(PublicationInputError):
    pass


def image_upload_file(source):
    with source.open('rb') as stream:
        magic = stream.read(16)
    suffix = '.png' if magic.startswith(b'\x89PNG\r\n\x1a\n') else '.jpg' if magic.startswith(b'\xff\xd8\xff') else None
    if suffix is None:
        raise PlatformFormError('B站封面仅支持PNG或JPEG，请重新选择图片')
    target = source.with_name(source.name + '-upload' + suffix)
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return target


def validate_submit_body(body, content, *, cid, filename, statement_id, cover_urls=None):
    """Compare the actual one-video POST, never patch the platform's request."""
    options = content['bilibili']
    videos = body.get('videos')
    copyright = 2 if options['statement'] == '内容为转载' else 1 if options.get('rights_reserved') else 3
    expected = {'title': content['title'], 'desc': content.get('description', ''),
                'tag': ','.join(options['tags']), 'human_type2': options['partition_id'],
                'copyright': copyright, 'no_reprint': int(options.get('rights_reserved', False)),
                'is_only_self': int(options['visibility'] == 'private')}
    if any(type(body.get(k)) is not type(v) or body[k] != v for k, v in expected.items()):
        raise PlatformFormError('平台实际提交内容与已确认配置不一致，已阻止投稿，请重新核对')
    if (body.get('creation_statement') != {'id': statement_id}
            or body.get('source', '') != options.get('source', '')
            or body.get('recreate') not in (-1, 0) or body.get('dtime', 0) != 0
            or body.get('aid') or body.get('mission_id') or body.get('topic_id')
            or body.get('porder') or body.get('adorder_id') or body.get('charging_pay')
            or body.get('staffs') or body.get('pay_season_id')):
        raise PlatformFormError('unexpected rights, scheduled, commercial or existing-publication settings')
    if (not isinstance(videos, list) or len(videos) != 1 or not isinstance(videos[0], dict)
            or type(videos[0].get('cid')) is not int or videos[0]['cid'] != cid
            or videos[0].get('filename') != filename):
        raise PlatformFormError('平台上传的视频与本次任务不一致，已阻止投稿')
    for key in ('cover', 'cover43'):
        value = body.get(key)
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (parsed is None or parsed.scheme not in {'http', 'https'}
                or not (parsed.hostname or '').endswith('.hdslb.com') or parsed.username
                or not cover_urls or value != cover_urls.get(key)):
            raise PlatformFormError('两张已确认封面尚未完成上传，已阻止投稿')


class BrowserPublisher:
    def __init__(self, *, timeout=900):
        self.timeout = timeout
        self.page = None
        self._protected = None
        self._stopped = lambda: False
        self._human = None
        self._submitted = False
        self._ready = False
        self._capture = {'count': 0, 'forwarded': 0}
        self._request = None
        self._payload = None
        self._cid = None
        self._filename = None
        self._statement_id = None
        self._pre = None
        self._types = None
        self._cover_fingerprint = None
        self._canvas_fingerprints = None
        self._cover_export_open = False
        self._cover_requests = {}
        self._cover_urls = {}

    def _check(self):
        if self._stopped() or self.page is None or self.page.is_closed():
            raise PlatformFormError('原发布现场已停止或关闭，请回到任务核实状态')
        location = urlsplit(self.page.url)
        if location.scheme != 'https' or location.netloc != 'member.bilibili.com':
            raise PlatformFormError('发布页面地址已变化，请核对原账号现场')
        challenge = self.page.locator('.geetest_panel:visible, .bili-captcha:visible, .bili-mini-login:visible')
        if challenge.count():
            if self._human is None or self._submitted:
                raise PlatformFormError('请在原账号窗口完成安全验证后继续原尝试')
            self._human(self.page.bring_to_front)
            if self._stopped() or self.page.is_closed() or challenge.count():
                raise PlatformFormError('安全验证尚未完成，请在原窗口处理')

    def _wait(self, condition, *, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            before = time.monotonic()
            self._check()
            deadline += time.monotonic() - before
            value = condition()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise PlatformFormError('平台上传或表单准备超时，请核对原窗口，勿手动投稿')
            self.page.wait_for_timeout(100)

    def protect_scene(self, page):
        if self._protected is page:
            return
        if self._protected is not None:
            raise PlatformFormError('original editor cannot be replaced')
        self._protected = page
        def guard(route):
            request = route.request
            url = urlsplit(request.url)
            if request.method != 'POST' or url.netloc != 'member.bilibili.com':
                return route.fallback()
            if url.path == '/x/vu/web/cover/up':
                try:
                    if not self._cover_export_open:
                        raise ValueError('cover export outside confirmed editor save')
                    kind = cover_upload_kind(request.header_value('content-type'), request.post_data_buffer)
                    if kind in self._cover_requests.values():
                        raise ValueError('duplicate cover export')
                    self._cover_requests[request] = kind
                except (ValueError, TypeError, UnicodeError):
                    route.abort()
                    return
                return route.fallback()
            if url.path == ADD_PATH:
                self._capture['count'] += 1
                try:
                    if not self._submitted or self._capture['count'] != 1 or self._capture['forwarded']:
                        raise PlatformFormError('submission not authorized or already attempted')
                    body = json.loads(request.post_data or '')
                    validate_submit_body(body, self._payload.content, cid=self._cid,
                                         filename=self._filename, statement_id=self._statement_id,
                                         cover_urls=self._cover_urls)
                except (ValueError, KeyError, TypeError, AttributeError, PlatformFormError):
                    route.abort()
                    return
                self._capture['forwarded'] += 1
                self._request = request
                return route.fallback()
            if (url.path.startswith('/x/vu/web/edit') or url.path.startswith('/x/vupre/web/draft/')
                    and url.path.rsplit('/', 1)[-1] in {'add', 'update', 'del'}):
                return route.abort()
            return route.fallback()
        page.route('**/*', guard)
        page.on('response', self._response)

    def _response(self, response):
        try:
            if response.request is self._request:
                self._capture.update(status=response.status, json=response.json())
                return
            url = urlsplit(response.url)
            if url.netloc != 'member.bilibili.com' or response.status != 200:
                return
            if response.request in self._cover_requests:
                data = response_data({'status': response.status, 'json': response.json()})
                value = data.get('url')
                parsed = urlsplit(value) if isinstance(value, str) else None
                if (parsed and parsed.scheme in {'http', 'https'}
                        and (parsed.hostname or '').endswith('.hdslb.com') and not parsed.username):
                    self._cover_urls[self._cover_requests[response.request]] = value
                return
            if url.path in {PRE_PATH, TYPE_PATH}:
                data = response_data({'status': response.status, 'json': response.json()})
                if url.path == PRE_PATH:
                    self._pre = data
                else:
                    self._types = data
            if url.path == '/preupload' and self._payload is not None:
                query = parse_qs(url.query)
                if query.get('name') != [self._payload.video.name] or query.get('size') != [str(self._payload.video.stat().st_size)]:
                    return
                body = response.json()
                cid = int(submission_id(body.get('biz_id')))
                uri = body.get('upos_uri')
                if not isinstance(uri, str) or not uri.startswith('upos://'):
                    return
                self._cid, self._filename = cid, Path(uri).stem
        except (ValueError, TypeError, KeyError, AttributeError):
            pass  # Missing proof is a hard stop, never permission to guess.

    def fill_form(self, page, payload, *, stopped, replace_matching_draft=False, wait_for_human=None):
        if self.page is not None or payload.space_cover is None:
            raise PlatformFormError('one editor and both confirmed covers are required')
        self.page, self._payload, self._stopped, self._human = page, payload, stopped, wait_for_human
        self.protect_scene(page)
        page.goto(CREATOR_ORIGIN + '/platform/upload/video/frame', wait_until='commit', timeout=60000)
        # This is an asynchronously mounted micro-app. Its usable upload
        # control is the readiness signal, not unrelated document resources.
        self._wait(lambda: page.get_by_text('上传视频', exact=True).is_visible(), timeout=120)
        # A local saved upload is separate from this new authorized upload.
        # Do not restore, discard, edit or submit it on behalf of this task.
        if page.get_by_text('继续编辑', exact=True).is_visible():
            raise PlatformFormError('浏览器中有未提交的视频，请先核对原稿件，勿重复上传或投稿')
        self._check()
        # The visible upload text has pointer-events:none on the live page.
        # Fill the actual upload component, never an image/cover file input.
        page.locator('.upload-wrp .upload input[type=file]').set_input_files(str(payload.video.resolve()))
        self._wait(lambda: self._cid is not None and page.get_by_text('上传完成', exact=True).count() >= 1)
        self._wait(lambda: self._pre is not None and self._types is not None)
        options = payload.content['bilibili']
        matching_partition(self._types, options['partition_id'], options['partition_name'])
        statements = (self._pre.get('neutral_mark') or {}).get('mark_list') or []
        if options['statement'] == '内容无需标注':
            self._statement_id = -1
        elif options['statement'] == '内容为转载':
            self._statement_id = -2
        else:
            matches = [r for r in statements if r.get('content') == options['statement'] and type(r.get('id')) is int]
            if len(matches) != 1:
                raise PlatformFormError('当前账号已无法选择原创作声明，请重新配置后核对')
            self._statement_id = matches[0]['id']
        page.get_by_placeholder('请输入稿件标题', exact=True).fill(payload.content['title'])
        page.locator('.desc-container .ql-editor').fill(payload.content.get('description', ''))
        page.locator('.video-human-type .select-controller').click(timeout=5000)
        page.locator('.human-type-list').get_by_text(options['partition_name'], exact=True).click(timeout=5000)
        tags = page.locator('.tag-pre-wrp .label-item-v2-container .close')
        for _ in range(10):
            if not tags.count():
                break
            self._check()
            tags.first.click(timeout=5000)
        tag_input = page.get_by_placeholder('按回车键Enter创建标签', exact=True)
        for tag in options['tags']:
            self._check()
            tag_input.fill(tag)
            tag_input.press('Enter')
        statement = page.get_by_placeholder('请选择符合您视频内容的创作声明', exact=True)
        statement.click(timeout=5000)
        page.get_by_text(options['statement'], exact=True).filter(visible=True).click(timeout=5000)
        # Reopen to explicitly set the independent content-rights checkbox.
        statement.click(timeout=5000)
        rights = page.get_by_text('内容为自制：未经作者允许，禁止转载', exact=True)
        if rights.is_visible():
            checked = 'option-text-selected' in (rights.get_attribute('class') or '').split()
            if checked != options.get('rights_reserved', False):
                page.locator('.auth-content').click(timeout=5000)
            if ('option-text-selected' in (rights.get_attribute('class') or '').split()) != options.get('rights_reserved', False):
                raise PlatformFormError('自制权利勾选未保持，已阻止投稿，请重新核对')
        elif options.get('rights_reserved'):
            raise PlatformFormError('当前页面没有对应的自制权利选项，已阻止投稿')
        page.get_by_placeholder('请输入稿件标题', exact=True).click(timeout=5000)
        if options['statement'] == '内容为转载':
            page.get_by_placeholder('转载视频请注明来源、时间、地点', exact=False).fill(options['source'])
        more = page.get_by_text('更多设置', exact=False).filter(visible=True)
        visibility = page.locator('.vu-only-self')
        if not visibility.is_visible():
            more.click(timeout=5000)
        visibility.get_by_text('公开可见' if options['visibility'] == 'public' else '仅自己可见', exact=True).click(timeout=5000)
        # Do not grant the separate video recreation agreement by default.
        recreate = page.get_by_text('允许二创', exact=True)
        if recreate.is_visible():
            box = page.locator('.video-recreate input[type=checkbox]')
            if box.is_checked():
                page.locator('.video-recreate .bcc-checkbox').click(timeout=5000)
            if box.is_checked():
                raise PlatformFormError('无法关闭二创授权，已阻止投稿，请核对原窗口')
        self._fill_covers(payload)
        self._ready = True
        self._assert_form()

    def _fill_covers(self, payload):
        self.page.locator('.cover-content .cover-module-main').click(timeout=5000)
        dialog = self.page.locator('.cover-editor:visible')
        dialog.wait_for(state='visible', timeout=10000)
        sync = dialog.locator('.sync-checkbox input[type=checkbox]')
        if sync.count() != 1:
            raise PlatformFormError('无法核实双封面同步设置，已阻止投稿')
        if sync.is_checked():
            dialog.locator('.sync-checkbox').click(timeout=5000)
        if sync.is_checked():
            raise PlatformFormError('双封面仍处于同步状态，请在原窗口核对')
        confirmed_main = None
        for selector, source in (('#editor_4_3', payload.cover), ('#editor_16_9', payload.space_cover)):
            self._check()
            dialog.locator(selector).click(timeout=5000)
            root = dialog.locator(selector)
            previous = self._cover_canvas(root)
            file_input = dialog.locator('.cover-upload input[type=file]')
            upload = image_upload_file(source)
            try:
                file_input.set_input_files(str(upload.resolve()))
                expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                selected = file_input.evaluate("""async e => {
                  if(e.files.length!==1)return '';const b=await e.files[0].arrayBuffer();
                  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',b))].map(x=>x.toString(16).padStart(2,'0')).join('');
                }""")
                if selected != expected_hash:
                    raise PlatformFormError('所选封面与任务冻结文件不一致，已阻止投稿')
                def decoded():
                    value = self._cover_canvas(root)
                    return value if value and value != previous else None
                current = self._wait(decoded, timeout=30)
            finally:
                upload.unlink(missing_ok=True)
            if selector == '#editor_4_3':
                confirmed_main = current
        self._canvas_fingerprints = self._cover_canvases(dialog)
        if self._canvas_fingerprints[0] != confirmed_main:
            raise PlatformFormError('空间封面覆盖了推荐封面，请关闭双比例同步并重新核对')
        self._cover_export_open = True
        try:
            dialog.get_by_text('完成', exact=True).click(timeout=10000)
            self._wait(lambda: not dialog.is_visible() and len(self._cover_urls) == 2, timeout=30)
        finally:
            self._cover_export_open = False
        if self._cover_urls['cover'] == self._cover_urls['cover43']:
            raise PlatformFormError('两张封面上传结果相同，已阻止投稿，请重新核对')
        self._cover_fingerprint = self._cover_state()
        if not self._cover_fingerprint:
            raise PlatformFormError('两张封面预览未能完整读取，请重新核对')

    def _cover_state(self):
        # Data/blob sources are local to the owned editor, never logged/reported.
        return self.page.locator('.cover-content .cover-module-main img').evaluate_all("""es => {
          const a=es.filter(e=>e.complete&&e.naturalWidth>100&&e.naturalHeight>100);
          return a.length===1 ? a.map(e=>e.src) : null;
        }""")

    @staticmethod
    def _cover_canvas(root):
        return root.evaluate("""async root => {
              const nodes=[...root.querySelectorAll('canvas.lower-canvas')].filter(e=>e.width>100&&e.height>100);
              if(nodes.length!==1)return null;
              const e=nodes[0],c=document.createElement('canvas');
              c.width=e.width||e.naturalWidth;c.height=e.height||e.naturalHeight;
              const x=c.getContext('2d');x.drawImage(e,0,0,c.width,c.height);
              const b=x.getImageData(0,0,c.width,c.height).data;
              const crop=root.querySelector('.crop-box-fixed');
              const geometry=crop ? [crop.style.left,crop.style.top,crop.style.width,crop.style.height] : [];
              return [c.width,c.height,geometry,[...new Uint8Array(await crypto.subtle.digest('SHA-256',b))].map(x=>x.toString(16).padStart(2,'0')).join('')];
            }""")

    def _cover_canvases(self, dialog):
        result = []
        for selector in ('#editor_4_3', '#editor_16_9'):
            self._check()
            root = dialog.locator(selector)
            root.click(timeout=5000)
            digest = self._cover_canvas(root)
            if not digest:
                raise PlatformFormError('无法核实独立封面画面，已阻止投稿，请检查原窗口')
            result.append(digest)
        return result

    def _assert_form(self):
        self._check()
        if not self._ready or self._capture['count']:
            raise PlatformFormError('form incomplete or already submitted')
        page, content = self.page, self._payload.content
        options = content['bilibili']
        visible = page.locator('.vu-only-self .check-radio-v2-container').filter(
            has=page.locator('.check-radio-v2-box-checked')).inner_text().strip()
        if (page.get_by_placeholder('请输入稿件标题', exact=True).input_value() != content['title']
                or page.locator('.desc-container .ql-editor').inner_text().rstrip('\n') != content.get('description', '')
                or page.locator('.video-human-type .select-item-cont').inner_text().strip() != options['partition_name']
                or page.locator('.tag-pre-wrp .label-item-v2-content').all_text_contents() != options['tags']
                or page.get_by_placeholder('请选择符合您视频内容的创作声明', exact=True).input_value() != options['statement']
                or visible != ('公开可见' if options['visibility'] == 'public' else '仅自己可见')
                or self._cover_state() != self._cover_fingerprint
                or page.locator('.cover-editor:visible').count()):
            raise PlatformFormError('提交前表单已变化，已阻止投稿，请重新核对内容')
        page.locator('.cover-content .cover-module-main').click(timeout=5000)
        dialog = page.locator('.cover-editor:visible')
        dialog.wait_for(state='visible', timeout=10000)
        if self._cover_canvases(dialog) != self._canvas_fingerprints:
            raise PlatformFormError('提交前封面画面已变化，已阻止投稿，请重新核对')
        dialog.get_by_text('取消', exact=True).click(timeout=5000)
        self._wait(lambda: not dialog.is_visible(), timeout=10)

    def submit(self):
        if self._submitted:
            raise PlatformFormError('本次投稿已尝试提交，不能重复点击，请核验原稿件')
        self._assert_form()
        self._submitted = True
        self.page.get_by_text('立即投稿', exact=True).click(timeout=10000)

    def read_receipt(self):
        if not self._submitted:
            raise PlatformFormError('no submission by this publisher')
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not self.page.is_closed():
            if 'json' in self._capture:
                try:
                    return accepted_receipt(self._capture)
                except BilibiliPublicationError:
                    raise PlatformFormError('未取得可信投稿回执，请在原任务核验，勿再次投稿') from None
            self.page.wait_for_timeout(100)
        raise PlatformFormError('投稿结果待核实，请在原任务核验，勿再次投稿')


class BilibiliPublisher:
    def __init__(self, endpoint, *, browser_session_factory=BrowserSession,
                 identity_reader=None, browser_factory=BrowserPublisher):
        self.endpoint = endpoint
        self.browser_session_factory = browser_session_factory
        self.identity_reader = identity_reader
        self.browser_factory = browser_factory
        self._started = False

    def _identity(self, page=None):
        if self.identity_reader:
            return self.identity_reader(self.endpoint)
        return read_bilibili_identity(self.endpoint, browser_context=page.context if page else None)

    def _verify(self, expected, page=None):
        observed = self._identity(page)
        if observed.get('login_status') != 'normal' or observed.get('platform_account_id') != expected:
            raise PublicationInputError('B站当前登录身份与原任务不一致或登录已失效，请核对原账号')

    def publish(self, payload, control, *, method=PublishMethod.BROWSER):
        if method != PublishMethod.BROWSER or self._started:
            raise PublicationInputError('publication method unavailable or instance already used')
        self._started = True
        control.check_stopped()
        observed = self._identity()
        ready = observed.get('login_status') == 'normal'
        if ready:
            self._verify(payload.platform_uid)
            control.on_prepared()
            control.check_stopped()
        elif control.wait_for_human is None:
            raise PublicationInputError('B站登录需要人工处理，请在原账号窗口完成验证')
        implementation = self.browser_factory()
        with self.browser_session_factory(self.endpoint) as session:
            def human(show):
                if control.wait_for_human is None:
                    raise PublicationInputError('请在原账号窗口完成登录或安全验证，再继续原尝试')
                control.wait_for_human(show)
                control.check_stopped()
                self._verify(payload.platform_uid, session.page)
            implementation.protect_scene(session.page)
            while not ready:
                control.check_stopped()
                session.page.goto(CREATOR_ORIGIN + '/platform/upload/video/frame', wait_until='commit', timeout=60000)
                control.wait_for_human(session.page.bring_to_front)
                control.check_stopped()
                self._verify(payload.platform_uid, session.page)
                ready = True
                control.on_prepared()
                control.check_stopped()
            implementation.fill_form(session.page, payload, stopped=control.stopped,
                replace_matching_draft=control.replace_matching_draft, wait_for_human=human)
            control.check_stopped()
            self._verify(payload.platform_uid, session.page)
            control.before_submit()
            control.check_stopped()
            implementation.submit()
            return implementation.read_receipt()
