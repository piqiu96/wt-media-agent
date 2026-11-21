"""Baidu's observed editor through Playwright; a receipt is not public success."""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from wt_media_agent.clients.baidu.identity import identify_baidu
from wt_media_agent.platforms.publish import PublicationInputError, PublishControl, PublishPayload, PublishReceipt
from wt_media_agent.runtime.constants import PublishMethod
from wt_media_agent.services.browser.cdp import read_live_cookies
from wt_media_agent.services.browser.session import BrowserSession


logger = logging.getLogger(__name__)


class PlatformFormError(PublicationInputError):
    pass


def publication_description(content):
    description = content.get('description')
    topics = content.get('topics', '')
    if not isinstance(description, str) or not description.strip() or not isinstance(topics, str):
        raise PlatformFormError('confirmed publication text is missing')
    value = description + (' ' + topics if topics else '')
    if len(value) > 50:
        raise PlatformFormError('confirmed description and topics exceed the platform editor limit')
    return value


def image_upload_file(source: Path) -> Path:
    # Frozen resources use an extensionless private path. Baidu checks the
    # browser file name/MIME; keep the exact bytes and give the copy its type.
    with source.open('rb') as stream:
        magic = stream.read(16)
    if magic.startswith(b'\xff\xd8\xff'):
        suffix = '.jpg'
    elif magic.startswith(b'\x89PNG\r\n\x1a\n'):
        suffix = '.png'
    elif magic[:4] == b'RIFF' and magic[8:12] == b'WEBP':
        suffix = '.webp'
    else:
        raise PlatformFormError('cover image type is not supported')
    target = source.with_name('cover-upload' + suffix)
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return target


def accepted_receipt(capture, expected_description):
    """Read only this editor's one video publish response, never save/upload."""
    if not isinstance(capture, dict) or capture.get('count') != 1:
        raise PlatformFormError('no unique publish request')
    if capture.get('title') != expected_description or capture.get('status') != 200:
        raise PlatformFormError('publish response does not match the confirmed content')
    try:
        body = json.loads(capture['text'])
        if type(body.get('errno')) not in (int, str) or str(body['errno']) != '0':
            raise ValueError('platform refused')
        ret = body['ret']
        value = ret.get('article_id') or ret.get('nid')
        if type(value) is int:
            value = str(value)
        if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
                or len(value) > 255 or int(value) <= 0 or str(int(value)) != value):
            raise ValueError('invalid receipt')
        return {'platform_work_id': value}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PlatformFormError('no credible accepted video receipt') from None


class BrowserPublisher:
    def __init__(self, *, timeout=900.0):
        self.page = None
        self.timeout = timeout
        self._stopped = lambda: False
        self._wait_for_human = None
        self._human_seconds = 0.0
        self._ready = False
        self._submitted = False
        self._description = None
        self._declaration = None
        self._covers = []
        self._cover_images = []
        self._image_responses = {}
        self._capture = {'count': 0}
        self._request = None
        self._diagnostic_path = None
        self._protected_page = None

    def _record_failure(self, filename=None):
        # A local, owner-private attempt artifact; never attach it to Cloud
        # reports or logs. Capture errors must not replace the execution error.
        if self._diagnostic_path is None:
            return
        try:
            data = self.page.screenshot(timeout=5000)
            destination = self._diagnostic_path.with_name(filename) if filename else self._diagnostic_path
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
            # Structural upload controls only: omit values, text, URLs and
            # file names. This distinguishes replaced/duplicate input nodes.
            controls = self.page.locator('input[type=file]').evaluate_all("""es => es.map(e => ({
                accept:e.accept, disabled:e.disabled,
                ancestors:[e,e.parentElement,e.parentElement?.parentElement,e.parentElement?.parentElement?.parentElement]
                    .filter(Boolean).map(n=>({tag:n.tagName,id:n.id,className:n.className,testId:n.getAttribute('data-testid')}))
            }))""")
            menu_structure = """es => es.map(e => {
                const nodes=[];for(let n=e;n && nodes.length<6;n=n.parentElement){
                    const s=getComputedStyle(n),r=n.getBoundingClientRect();
                    nodes.push({tag:n.tagName,id:n.id,className:n.className,
                        display:s.display,visibility:s.visibility,opacity:s.opacity,pointerEvents:s.pointerEvents,
                        rect:{x:Math.round(r.x),y:Math.round(r.y),width:Math.round(r.width),height:Math.round(r.height)}});
                }return nodes;
            })"""
            details = {'file_inputs':controls,
                'publish_menu':self.page.locator('#home-publish-btn').evaluate_all(menu_structure),
                'video_entry':self.page.get_by_text('发布视频',exact=True).evaluate_all(menu_structure)}
            descriptor = os.open(destination.with_suffix('.json'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(details,stream)
        except Exception as error:
            logger.info('baidu_publication_diagnostic outcome=failed error_type=%s', type(error).__name__)

    def _check(self):
        if self._stopped():
            raise PlatformFormError('publication execution stopped')
        if self.page is None or self.page.is_closed():
            raise PlatformFormError('no owned editor')
        url = urlsplit(self.page.url)
        if url.scheme != 'https' or url.netloc != 'baijiahao.baidu.com':
            raise PlatformFormError('editor origin changed')
        while self.page.locator('.passMod_dialog-wrapper:visible').count():
            if self._submitted or self._wait_for_human is None:
                raise PlatformFormError('platform requires human security verification')
            start = time.monotonic()
            self._wait_for_human(self.page.bring_to_front)
            self._human_seconds += time.monotonic() - start
            if self._stopped() or self.page.is_closed():
                raise PlatformFormError('original human scene stopped')
            url = urlsplit(self.page.url)
            if url.scheme != 'https' or url.netloc != 'baijiahao.baidu.com':
                raise PlatformFormError('editor origin changed')
        return url.path

    def _wait(self, condition, *, timeout=None, stage=None, cover_index=None):
        # Fixed internal stage names only: never log editor content, URLs,
        # file paths, or exception messages from the platform/browser.
        if stage:
            logger.info('baidu_publication_wait stage=%s cover_index=%s outcome=started', stage, cover_index)
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        try:
            while True:
                human_before = self._human_seconds
                self._check()
                deadline += self._human_seconds - human_before
                value = condition()
                if value:
                    if stage:
                        logger.info('baidu_publication_wait stage=%s cover_index=%s outcome=completed', stage, cover_index)
                    return value
                if time.monotonic() >= deadline:
                    raise PlatformFormError('platform editor did not become ready')
                self.page.wait_for_timeout(100)  # dispatch response events while polling
        except Exception as error:
            self._record_failure()
            if stage:
                logger.info('baidu_publication_wait stage=%s cover_index=%s outcome=failed error_type=%s',
                            stage, cover_index, type(error).__name__)
            raise

    def fill_form(self, page, payload, *, stopped, replace_matching_draft=False, wait_for_human=None):
        self._wait_for_human = wait_for_human
        try:
            self._fill_form(page, payload, stopped=stopped, replace_matching_draft=replace_matching_draft)
        except Exception:
            self._record_failure()
            raise

    def _fill_form(self, page, payload, *, stopped, replace_matching_draft=False):
        if self.page is not None:
            raise PlatformFormError('form preparation cannot be replayed')
        self.page, self._stopped = page, stopped
        self._diagnostic_path = payload.video.parent / 'editor-failure.png'
        if stopped():
            raise PlatformFormError('publication stopped before editor')
        self.protect_scene(page)
        page.goto('https://baijiahao.baidu.com/builder/rc/home', wait_until='domcontentloaded')
        self._check()
        video_entry = page.get_by_text('发布视频', exact=True)
        publish_menu = page.locator('#home-publish-btn')
        self._wait(lambda: video_entry.is_visible() or publish_menu.is_visible(), timeout=min(self.timeout, 15))
        # Transparent collapsed menu items still satisfy is_visible(). Open
        # the observed publish menu whenever it is present before clicking.
        if publish_menu.is_visible():
            self._check()
            publish_menu.hover(timeout=5000)
            self._wait(video_entry.is_visible, timeout=min(self.timeout, 15))
        self._check()
        # Wait for the portal's actual placement and hit target. Visibility
        # alone can precede placement; auto-scrolling then dismisses the menu.
        self._wait(lambda: video_entry.evaluate("""e => {
            const r=e.getBoundingClientRect();
            if(r.width<=0 || r.height<=0 || r.left<0 || r.top<0 || r.right>innerWidth || r.bottom>innerHeight) return false;
            const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
            return hit===e || e.contains(hit);
        }"""),timeout=min(self.timeout,15),stage='video_entry_ready')
        self._record_failure(filename='publish-menu.png')
        video_entry.click(timeout=15000)
        self._wait(lambda: self._check() == '/builder/rc/edit'
                   and page.locator('input[type=file][accept*=".mp4"]').count())
        editor = page.locator('[contenteditable=true]')
        prior_sources = None
        if editor.count():
            # Baidu restores an unsent editor across browser restarts. Preserve
            # other drafts; a matching one still needs a fresh video upload,
            # full field verification and the current attempt's submit grant.
            if (replace_matching_draft is not True or editor.count() != 1
                    or editor.inner_text() != publication_description(payload.content)):
                self._record_failure()
                raise PlatformFormError('new editor unexpectedly contains a prior video')
            prior_sources = page.locator('video').evaluate_all('(es) => es.map(e => e.currentSrc || e.src).filter(Boolean)')
            if not prior_sources:
                raise PlatformFormError('restored video cannot be identified for replacement')
        self._check()
        video_input = page.locator('input[type=file][accept*=".mp4"]')
        if prior_sources is not None:
            # The restored editor exposes both Replace and Add-video inputs.
            # Select this video's action, never the separate .cover-add input.
            video_input = page.locator('#cover-tabs-container > :not(.cover-add) input[type=file][accept*=".mp4"]')
        video_input.set_input_files(str(payload.video.resolve()))
        if prior_sources is not None:
            self._wait(lambda: self._upload_ready() and page.locator('video').evaluate_all(
                '(es, previous) => es.some(e => (e.currentSrc || e.src) && !previous.includes(e.currentSrc || e.src))',
                prior_sources), stage='restored_video_replaced')
        else:
            self._wait(self._upload_ready)
        self.fill_fields(page, payload, stopped=stopped)

    def _upload_ready(self):
        upload = self.page.locator('#cover-tabs-container')
        if upload.count() != 1:
            return False
        # Descriptions and their preview may legitimately contain percentages
        # or phrases such as "上传中". Read only the observed video upload area.
        body = upload.inner_text()
        if '上传失败' in body or '转码失败' in body:
            raise PlatformFormError('platform video upload failed')
        if ('上传中' in body or '转码中' in body
                or any(int(pct) < 100 for pct in re.findall(r'(?<!\d)(\d{1,3})\s*%', body))):
            return False
        return (self.page.locator('[contenteditable=true]').count() == 1
                and self.page.locator('[data-testid=cover-preview], [data-testid=select-cover]').count() == 2
                and self.page.get_by_role('button', name='发布', exact=True).is_enabled())

    def fill_fields(self, page, payload, *, stopped):
        if self._ready or self._submitted:
            raise PlatformFormError('form preparation cannot be replayed')
        self.page, self._stopped = page, stopped
        self.protect_scene(page)
        if self._check() != '/builder/rc/edit':
            raise PlatformFormError('not the video editor')
        self._description = publication_description(payload.content)
        page.on('response', self._remember_image_response)
        self._declaration = payload.content.get('declaration')
        if not isinstance(self._declaration, str) or not self._declaration:
            raise PlatformFormError('confirmed declaration is missing')
        onboarding = page.get_by_role('button', name='我知道了', exact=True).filter(visible=True)
        for _ in range(4):
            if not onboarding.count() or not onboarding.first.evaluate(
                    "e => getComputedStyle(e).opacity !== '0' && getComputedStyle(e).pointerEvents !== 'none'"):
                break
            self._check()
            onboarding.click(timeout=5000)
        self._check()
        page.locator('[contenteditable=true]').fill(self._description)
        page.get_by_placeholder('请选择创作声明', exact=True).click(timeout=5000)
        dialog = page.locator('[role=dialog]:visible')
        dialog.get_by_text(self._declaration, exact=True).click(timeout=5000)
        self._check()
        dialog.get_by_role('button', name='确定', exact=True).click(timeout=5000)
        self._wait(lambda: page.get_by_placeholder('请选择创作声明', exact=True).input_value() == self._declaration)
        upload = image_upload_file(payload.cover)
        for index in range(2):
            self._check()
            cover_control = page.locator('[data-testid=cover-preview], [data-testid=select-cover]').nth(index)
            previous_preview = cover_control.locator('img').evaluate_all('(es) => es.map(e => e.src)')
            cover_control.click(timeout=5000)
            dialog.wait_for(state='visible', timeout=5000)
            file_input = dialog.locator('input[type=file][accept="image/*"]').last
            # The modal also creates blob thumbnails from the uploaded video.
            # Only a decoded image in this file input's upload control proves
            # that the selected local cover was read successfully.
            upload_control = file_input.locator('..')
            previous = upload_control.locator('img').evaluate_all('(es) => es.map(e => e.src)')
            self._check()
            file_input.set_input_files(str(upload.resolve()))
            self._wait(lambda: upload_control.locator('img').evaluate_all(
                '(es, previous) => es.some(e => e.src.startsWith("blob:") && !previous.includes(e.src) && e.complete && e.naturalWidth > 0)', previous),
                stage='cover_upload_decode', cover_index=index)
            self._record_failure(filename=f'cover-selection-{index}.png')
            self._check()
            dialog.get_by_role('button', name='确定', exact=True).click(timeout=5000)
            self._wait(lambda: dialog.count() == 0, stage='cover_dialog_closed', cover_index=index)
            preview = cover_control.locator('img')
            source_kind = preview.evaluate_all("(es) => es.length !== 1 ? 'missing_or_multiple' : es[0].src.startsWith('blob:') ? 'temporary' : es[0].src.startsWith('https://') ? 'uploaded' : 'other'")
            logger.info('baidu_publication_cover_preview cover_index=%s source_kind=%s', index, source_kind)
            # Baidu first renders a local blob, then replaces it with the
            # uploaded cover URL. Freeze only the decoded HTTPS image in this
            # exact slot; a temporary preview is not upload completion.
            # Missing images keep polling without hiding later stop signals.
            value = self._wait(lambda: preview.evaluate_all(
                '(es, previous) => es.length === 1 && es[0].src.startsWith("https://") && es[0].complete && es[0].naturalWidth > 0 && !previous.includes(es[0].src) ? es[0].src : null',
                previous_preview), stage='cover_preview_replaced', cover_index=index)
            self._covers.append(value)
            self._cover_images.append(self._cover_image(preview))
        self._ready = True
        self._assert_form()

    def _remember_image_response(self, response):
        if (response.request.resource_type != 'image'
                and not response.headers.get('content-type', '').lower().startswith('image/')):
            return
        self._image_responses[response.url] = response
        previous = response.request.redirected_from
        while previous is not None:
            self._image_responses[previous.url] = response
            previous = previous.redirected_from

    def _cover_image(self, image):
        # Reuse the image this browser already loaded, including its Profile
        # proxy/session. URL rotation is allowed only for identical image bytes.
        # Hashes stay in memory; no image/URL enters reports or logs.
        previous_state = None
        def loaded_source():
            nonlocal previous_state
            state = image.evaluate('(e) => ({source:e.src, ready:e.complete && e.naturalWidth > 0})')
            source = state['source']
            kind = ('https' if source.startswith('https://') else 'local' if source.startswith(('data:image/', 'blob:')) else 'other')
            observed = urlsplit(source)._replace(fragment='').geturl() in self._image_responses
            signature = (kind, state['ready'], observed)
            if signature != previous_state:
                logger.info('baidu_publication_cover_source kind=%s decoded=%s response_observed=%s', *signature)
                previous_state = signature
            if not state['ready']:
                return None
            if source.startswith(('data:image/', 'blob:')):
                return source
            if source.startswith('https://') and urlsplit(source)._replace(fragment='').geturl() in self._image_responses:
                return source
            return None
        source = self._wait(loaded_source, timeout=min(self.timeout, 10), stage='cover_image_loaded')
        if source.startswith(('data:image/', 'blob:')):
            # The editor can replace an uploaded image with its local copy.
            # Read bytes in the owning page, without another network request.
            return bytes(image.evaluate("""async e => {
                const response = await fetch(e.src);
                if (!response.ok || !(response.headers.get('content-type') || '').startsWith('image/')) throw new Error('invalid cover image');
                const data = await response.arrayBuffer();
                if (!data.byteLength || data.byteLength > 32 * 1024 * 1024) throw new Error('invalid cover size');
                return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', data)));
            }"""))
        response = self._image_responses.get(urlsplit(source)._replace(fragment='').geturl())
        if not source.startswith('https://') or response is None:
            raise PlatformFormError('uploaded cover response was not observed')
        if not response.ok or not response.headers.get('content-type', '').lower().startswith('image/'):
            raise PlatformFormError('confirmed cover cannot be read')
        data = response.body()
        if not data or len(data) > 32 * 1024 * 1024:
            raise PlatformFormError('confirmed cover image is invalid')
        return hashlib.sha256(data).digest()

    def _assert_form(self, *, expected_dialog=None):
        if not self._ready or self._check() != '/builder/rc/edit' or len(self._covers) != 2:
            raise PlatformFormError('form was not fully verified')
        if not self._upload_ready():
            raise PlatformFormError('platform video upload is not complete')
        page = self.page
        dialogs = page.locator('[role=dialog]:visible')
        dialog_changed = (dialogs.count() != 0 if expected_dialog is None
                          else dialogs.count() != 1 or expected_dialog.count() != 1)
        text_changed = page.locator('[contenteditable=true]').inner_text() != self._description
        declaration_changed = page.get_by_placeholder('请选择创作声明', exact=True).input_value() != self._declaration
        images = page.locator('[data-testid=cover-preview] img')
        covers_changed = (images.count() != 2 or len(self._cover_images) != 2
                          or [self._cover_image(images.nth(index)) for index in range(2)] != self._cover_images)
        if text_changed or declaration_changed or covers_changed or dialog_changed:
            # Identify the failing invariant without exposing content or signed URLs.
            logger.info('baidu_publication_form_check text_changed=%s declaration_changed=%s covers_changed=%s dialog_changed=%s',
                        text_changed, declaration_changed, covers_changed, dialog_changed)
            raise PlatformFormError('confirmed form changed before submission')

    @staticmethod
    def _is_publish(request):
        url = urlsplit(request.url)
        return (request.method == 'POST' and url.scheme == 'https' and url.netloc == 'baijiahao.baidu.com'
                and url.path == '/pcui/article/publish' and parse_qs(url.query).get('type') == ['video'])

    def protect_scene(self,page):
        if self._protected_page is page:
            return
        if self._protected_page is not None:
            raise PlatformFormError('original publication scene cannot be replaced')
        self._protected_page=page
        def requested(request):
            if not self._submitted or not self._is_publish(request):
                return
            self._capture['count'] += 1
            if self._capture['count'] != 1:
                return
            self._request = request
            raw = request.post_data or ''
            try:
                data = json.loads(raw) if raw.lstrip().startswith('{') else {k: v[0] for k, v in parse_qs(raw).items()}
                self._capture['title'] = data.get('title')
            except (ValueError, TypeError, AttributeError):
                pass

        def responded(response):
            if response.request is self._request:
                try:
                    self._capture.update(status=response.status, text=response.text()[:1000000])
                except Exception:
                    pass  # response loss leaves an unknown result, never permission to resubmit
        forwarded = 0

        def guard(route):
            nonlocal forwarded
            if not self._is_publish(route.request):
                route.fallback()
            elif not self._submitted or forwarded:
                route.abort()
            else:
                forwarded += 1
                route.fallback()

        page.route('https://baijiahao.baidu.com/pcui/article/publish?*', guard)
        page.on('request', requested)
        page.on('response', responded)

    def submit(self):
        if self._submitted:
            raise PlatformFormError('final submission cannot be repeated')
        self._assert_form()
        self._submitted = True
        self.page.get_by_role('button', name='发布', exact=True).click(timeout=5000)
        confirmation = self.page.locator('[role=dialog]:visible').filter(
            has=self.page.get_by_text('作品描述中存在未匹配的话题/@内容', exact=True))
        self._wait(lambda: self._capture['count'] or confirmation.count() == 1, timeout=15)
        if not self._capture['count']:
            # Keep the confirmed literal text; automatic matching can replace
            # topics or mentions. Continue this click, never click Publish again.
            self._assert_form(expected_dialog=confirmation)
            confirmation.get_by_role('button', name='不处理', exact=True).click(timeout=5000)

    def read_receipt(self):
        if not self._submitted:
            raise PlatformFormError('no submission by this publisher')
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self._capture.get('text') is not None:
                return accepted_receipt(self._capture, self._description)
            if self.page.is_closed():
                break
            self.page.wait_for_timeout(100)
        raise PlatformFormError('platform receipt is unknown')



def read_baidu_identity(endpoint):
    """Compose local Cookie access and the existing basic identity API client."""
    return identify_baidu(read_live_cookies(endpoint))


class BaiduPublisher:
    """One publication, with an explicit implementation and mandatory controls.

    Browser details and Cloud task authority stay behind separate boundaries.
    A used instance is not a restart/retry capability.
    """

    def __init__(self, endpoint, *, browser_session_factory=BrowserSession,
                 identity_reader=None, browser_factory=None):
        self.endpoint = endpoint
        self.browser_session_factory = browser_session_factory
        self.identity_reader = identity_reader or read_baidu_identity
        self.browser_factory = browser_factory or BrowserPublisher
        self._started = False

    def _verify_identity(self, expected_uid):
        observed = self.identity_reader(self.endpoint)
        if (observed.get('login_status') != 'normal'
                or observed.get('platform_account_id') != expected_uid):
            raise PublicationInputError('live platform UID differs or login unavailable')

    def publish(self, payload: PublishPayload, control: PublishControl, *,
                method: PublishMethod = PublishMethod.BROWSER) -> PublishReceipt:
        if method != PublishMethod.BROWSER:
            raise PublicationInputError('publication implementation is not supported')
        if self._started:
            raise PublicationInputError('publication instance cannot be replayed')
        if not isinstance(payload.platform_uid, str) or not payload.platform_uid:
            raise PublicationInputError('expected platform UID is required')
        self._started = True
        control.check_stopped()
        observed = self.identity_reader(self.endpoint)
        initially_ready = observed.get('login_status') == 'normal'
        if initially_ready:
            if observed.get('platform_account_id') != payload.platform_uid:
                raise PublicationInputError('live platform UID differs')
            control.on_prepared()
            control.check_stopped()
        elif control.wait_for_human is None:
            raise PublicationInputError('platform login requires human verification')
        implementation = self.browser_factory()
        def human(show):
            if control.wait_for_human is None:
                raise PublicationInputError('human continuation is unavailable')
            control.wait_for_human(show)
            control.check_stopped()
            self._verify_identity(payload.platform_uid)
        with self.browser_session_factory(self.endpoint) as session:
            implementation.protect_scene(session.page)
            while observed.get('login_status') != 'normal':
                if control.wait_for_human is None:
                    raise PublicationInputError('platform login requires human verification')
                control.check_stopped()
                session.page.goto('https://baijiahao.baidu.com/builder/rc/home',wait_until='domcontentloaded')
                control.wait_for_human(session.page.bring_to_front)
                control.check_stopped()
                observed = self.identity_reader(self.endpoint)
            if observed.get('platform_account_id') != payload.platform_uid:
                raise PublicationInputError('live platform UID differs')
            if not initially_ready:
                control.on_prepared()
                control.check_stopped()
            implementation.fill_form(session.page, payload, stopped=control.stopped,
                                     replace_matching_draft=control.replace_matching_draft, wait_for_human=human)
            control.check_stopped()
            self._verify_identity(payload.platform_uid)
            control.check_stopped()
            control.before_submit()
            control.check_stopped()
            implementation.submit()
            return implementation.read_receipt()
