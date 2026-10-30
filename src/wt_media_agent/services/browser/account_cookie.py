"""Live, platform-scoped Cookie operations. Cloud owns the operation lifecycle."""
from __future__ import annotations
import hashlib
import json
import math
from . import cdp

DOMAINS = {'bilibili': ('bilibili.com', 'biliapi.net', 'biliapi.com'), 'baidu': ('baidu.com',), 'douyin': ('douyin.com',)}
FIELDS = ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite', 'priority', 'sameParty', 'sourceScheme', 'sourcePort', 'partitionKey')

def belongs(cookie, platform):
    host = str(cookie.get('domain', '')).lower().lstrip('.')
    return any(host == d or host.endswith('.' + d) for d in DOMAINS.get(platform, ()))

def scoped(cookies, platform):
    return [c for c in cookies if belongs(c, platform)]

def fingerprint(cookies):
    values = [{k: c[k] for k in FIELDS if k in c} for c in cookies]
    values.sort(key=lambda c: (c.get('domain', ''), c.get('path', '/'), c.get('name', ''), json.dumps(c.get('partitionKey'), sort_keys=True)))
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

def parse_input(platform, raw):
    if platform not in DOMAINS or not isinstance(raw, str) or not raw.strip() or len(raw.encode()) > 1048576:
        raise ValueError('请输入有效 Cookie')
    raw = raw.strip()
    if raw.startswith(('[', '{')):
        value = json.loads(raw)
        cookies = value.get('cookies') if isinstance(value, dict) else value
        if not isinstance(cookies, list) or not cookies:
            raise ValueError('Cookie JSON 需要非空数组')
    else:
        cookies = []
        for pair in raw.split(';'):
            if not pair.strip(): continue
            name, sep, value = pair.strip().partition('=')
            if not sep or not name.strip(): raise ValueError('Cookie 格式不正确')
            cookies.append({'name': name.strip(), 'value': value, 'domain': '.' + DOMAINS[platform][0], 'path': '/', 'secure': True})
    result = []
    seen = set()
    for c in cookies:
        if not isinstance(c, dict) or not isinstance(c.get('name'), str) or not isinstance(c.get('value'), str) or not belongs(c, platform):
            raise ValueError('Cookie 域名或格式与平台不匹配')
        if any(ch in c['name'] for ch in '\r\n;=') or any(ch in c['value'] for ch in '\r\n'):
            raise ValueError('Cookie 格式不正确')
        item = {k: c[k] for k in FIELDS if k in c}
        item['path'] = item.get('path') or '/'
        if not isinstance(item['path'], str) or not item['path'].startswith('/'):
            raise ValueError('Cookie 路径不正确')
        if 'expires' in item and (not isinstance(item['expires'], (int, float)) or not math.isfinite(item['expires'])):
            raise ValueError('Cookie 有效期不正确')
        if item.get('sameSite') not in (None, 'Strict', 'Lax', 'None'):
            raise ValueError('Cookie sameSite 不正确')
        for flag in ('secure', 'httpOnly'):
            if flag in item and not isinstance(item[flag], bool): raise ValueError('Cookie 属性不正确')
        key = (item['domain'], item['path'], item['name'], json.dumps(item.get('partitionKey'), sort_keys=True))
        if key in seen: raise ValueError('Cookie 存在重复项')
        seen.add(key); result.append(item)
    if not result: raise ValueError('Cookie 不能为空')
    return result

def matches_input(actual, incoming):
    values = {(c.get('domain'), c.get('path', '/'), c.get('name'), json.dumps(c.get('partitionKey'), sort_keys=True)): c.get('value') for c in actual}
    return all(values.get((c['domain'], c.get('path', '/'), c['name'], json.dumps(c.get('partitionKey'), sort_keys=True))) == c['value'] for c in incoming)

def execute(bitbrowser, stage, body, identify):
    platform = str(body.get('platform', ''))
    if platform not in DOMAINS or not body.get('operation_id') or not body.get('profile_id') or stage not in ('prepare', 'apply', 'inspect'):
        raise ValueError('Cookie 操作参数不完整')
    incoming = parse_input(platform, body.get('input_cookie', ''))
    endpoint = bitbrowser.open_profile_with_devtools(str(body['profile_id']))
    if not endpoint: raise cdp.CDPError('未取得浏览器实时连接')
    all_before = cdp.read_live_cookies(endpoint)
    before = scoped(all_before, platform)
    current_fp = fingerprint(before)
    if stage == 'prepare':
        identity = identify(platform, before, endpoint)
        before = scoped(cdp.read_live_cookies(endpoint), platform)
        current_fp = fingerprint(before)
        return {**identity, 'status': 'prepared', 'before_cookie': before, 'fingerprint': current_fp}
    expected_id = str(body.get('expected_platform_account_id') or '')
    if stage == 'apply':
        if not body.get('fingerprint') or body['fingerprint'] != current_fp:
            return {'status': 'failed', 'applied': False, 'message': '窗口登录状态已变化，请重新发起更新'}
        identity = identify(platform, before, endpoint)
        observed = str(identity.get('platform_account_id') or '')
        safe = identity.get('login_status') in ('normal', 'not_logged_in', 'expired') and ((expected_id and (not observed or observed == expected_id)) or (not expected_id and not observed))
        if not safe:
            return {'status': 'failed', 'applied': False, 'message': '窗口当前账号不一致或无法确认，请检查后再更新'}
        # Identity reads may refresh session Cookies; compare the actual snapshot
        # immediately before the write, after the platform request has finished.
        all_before = cdp.read_live_cookies(endpoint)
        if fingerprint(scoped(all_before, platform)) != body['fingerprint']:
            return {'status': 'failed', 'applied': False, 'message': '窗口登录状态已变化，请重新发起更新'}
        try:
            cdp.write_cookies(endpoint, incoming)
            all_after = cdp.read_live_cookies(endpoint)
        except Exception:
            return {'status': 'uncertain', 'applied': True, 'message': '更新结果待确认，请核实当前登录状态'}
        if fingerprint([c for c in all_before if not belongs(c, platform)]) != fingerprint([c for c in all_after if not belongs(c, platform)]):
            return {'status': 'uncertain', 'applied': True, 'message': '窗口状态有变化，请核实更新结果'}
        actual = scoped(all_after, platform)
    else:
        actual = before
    identity = identify(platform, actual, endpoint)
    input_matches = matches_input(actual, incoming)
    success = input_matches and identity.get('login_status') == 'normal' and bool(identity.get('platform_account_id')) and (not expected_id or str(identity['platform_account_id']) == expected_id)
    if success:
        return {**identity, 'status': 'applied', 'applied': True, 'input_matches': True, 'cookies': actual, 'message': 'Cookie 已更新并确认登录'}
    if stage == 'inspect' and body.get('fingerprint') == current_fp and not input_matches:
        return {'status': 'failed', 'applied': False, 'message': '确认未写入，可重新发起更新'}
    known = identity.get('login_status') in ('not_logged_in', 'expired') or (identity.get('login_status') == 'normal' and bool(identity.get('platform_account_id')))
    if stage == 'inspect' and known:
        return {**identity, 'status': 'failed', 'applied': True, 'verified': True, 'message': '已核实，本次 Cookie 未确认生效；请核对账号后重新更新'}
    return {**identity, 'status': 'uncertain', 'applied': True, 'input_matches': input_matches, 'message': '登录状态待确认，请在原窗口完成登录后核实'}
