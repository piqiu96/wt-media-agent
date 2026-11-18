"""Baidu's observed editor through Playwright; a receipt is not public success."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

class PlatformFormError(RuntimeError):
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


class BaiduPublisher:
    def __init__(self, *, timeout=900.0):
        self.page = None
        self.timeout = timeout
        self._stopped = lambda: False
        self._ready = False
        self._submitted = False
        self._description = None
        self._declaration = None
        self._covers = []
        self._capture = {'count': 0}
        self._request = None

    def _check(self):
        if self._stopped():
            raise PlatformFormError('publication execution stopped')
        if self.page is None or self.page.is_closed():
            raise PlatformFormError('no owned editor')
        url = urlsplit(self.page.url)
        if url.scheme != 'https' or url.netloc != 'baijiahao.baidu.com':
            raise PlatformFormError('editor origin changed')
        if self.page.locator('.passMod_dialog-wrapper:visible').count():
            raise PlatformFormError('platform requires human security verification')
        return url.path

    def _wait(self, condition, *, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            self._check()
            value = condition()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise PlatformFormError('platform editor did not become ready')
            self.page.wait_for_timeout(100)  # dispatch response events while polling

    def fill_form(self, page, payload, *, stopped):
        if self.page is not None:
            raise PlatformFormError('form preparation cannot be replayed')
        self.page, self._stopped = page, stopped
        if stopped():
            raise PlatformFormError('publication stopped before editor')
        page.goto('https://baijiahao.baidu.com/builder/rc/home', wait_until='domcontentloaded')
        self._check()
        page.get_by_text('发布视频', exact=True).click(timeout=15000)
        self._wait(lambda: self._check() == '/builder/rc/edit'
                   and page.locator('input[type=file][accept*=".mp4"]').count())
        if page.locator('[contenteditable=true]').count():
            raise PlatformFormError('new editor unexpectedly contains a prior video')
        self._check()
        page.locator('input[type=file][accept*=".mp4"]').set_input_files(str(payload.video.resolve()))
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
        if self._check() != '/builder/rc/edit':
            raise PlatformFormError('not the video editor')
        self._description = publication_description(payload.content)
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
            page.locator('[data-testid=cover-preview], [data-testid=select-cover]').nth(index).click(timeout=5000)
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
                '(es, previous) => es.some(e => e.src.startsWith("blob:") && !previous.includes(e.src) && e.complete && e.naturalWidth > 0)', previous))
            self._check()
            dialog.get_by_role('button', name='确定', exact=True).click(timeout=5000)
            self._wait(lambda: dialog.count() == 0)
            preview = page.locator('[data-testid=cover-preview] img').nth(index)
            value = self._wait(lambda: preview.evaluate('e => e.complete && e.naturalWidth > 0 ? e.src : null'))
            self._covers.append(value)
        self._ready = True
        self._assert_form()
        self._arm_receipt()

    def _assert_form(self, *, expected_dialog=None):
        if not self._ready or self._check() != '/builder/rc/edit' or len(self._covers) != 2:
            raise PlatformFormError('form was not fully verified')
        if not self._upload_ready():
            raise PlatformFormError('platform video upload is not complete')
        page = self.page
        dialogs = page.locator('[role=dialog]:visible')
        dialog_changed = (dialogs.count() != 0 if expected_dialog is None
                          else dialogs.count() != 1 or expected_dialog.count() != 1)
        if (page.locator('[contenteditable=true]').inner_text() != self._description
                or page.get_by_placeholder('请选择创作声明', exact=True).input_value() != self._declaration
                or page.locator('[data-testid=cover-preview] img').evaluate_all('(es) => es.map(e => e.src)') != self._covers
                or dialog_changed):
            raise PlatformFormError('confirmed form changed before submission')

    @staticmethod
    def _is_publish(request):
        url = urlsplit(request.url)
        return (request.method == 'POST' and url.scheme == 'https' and url.netloc == 'baijiahao.baidu.com'
                and url.path == '/pcui/article/publish' and parse_qs(url.query).get('type') == ['video'])

    def _arm_receipt(self):
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

        self.page.route('https://baijiahao.baidu.com/pcui/article/publish?*', guard)
        self.page.on('request', requested)
        self.page.on('response', responded)

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
