"""Original, still-live publication scene actions; never creates a browser."""
from wt_media_agent.clients.cloud.errors import (
    AgentAuthError, CloudError, CloudUnavailableError,
)
from wt_media_agent.local_api.publication_draft import refusal
from wt_media_agent.storage.publication import JournalConflict


def canonical(value):
    return (isinstance(value, str) and 0 < len(value) <= 20
            and value.isascii() and value.isdecimal()
            and str(int(value)) == value and 0 < int(value) <= 18446744073709551615)


def publication_run_response(job, token, user, attempt, operation, body):
    if not canonical(user):
        return refusal(403, 'publication_identity_required', '请重新登录后处理原发布现场')
    if operation not in {'scene', 'resume'}:
        return refusal(404, 'publication_scene_unavailable', '原发布现场不可用，请核实原任务结果')
    if (not canonical(attempt) or not isinstance(body, dict)
            or set(body) != {'publication_id', 'expected_revision'}
            or not all(canonical(body[key]) for key in body)):
        return refusal(400, 'publication_input_invalid', '任务、尝试或修订无效，请刷新后重试')
    if job is None or not token:
        return refusal(503, 'publication_scene_unavailable', '本机发布执行暂不可用，请保留原任务')
    try:
        result = job.handle_human_action(
            token, user, body['publication_id'], attempt, body['expected_revision'],
            resume=operation == 'resume',
        )
        return 200, {'data': result}
    except AgentAuthError:
        return refusal(403, 'publication_identity_required', '当前登录已失效，请重新登录后核实原任务')
    except (JournalConflict, CloudError):
        return refusal(409, 'publication_scene_unavailable', '原现场或任务已变化，请刷新并核实原任务结果')
    except (CloudUnavailableError, ConnectionError, TimeoutError):
        return refusal(503, 'publication_scene_unavailable', '未收到继续确认，请保留原页面并重试')
