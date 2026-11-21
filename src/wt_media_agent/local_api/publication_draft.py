"""Controlled draft input; caller identity is supplied by the native bridge."""
import logging
from sqlalchemy.exc import SQLAlchemyError
from wt_media_agent.storage.publication_draft import DraftConflict

logger = logging.getLogger(__name__)


def refusal(status, code, message):
    return status, {'error': {'code': code, 'message': message, 'retryable': False, 'recovery_action': ''}}


def publication_draft_response(store, method, draft_type, user_id, body):
    if not store:
        return refusal(503, 'draft_unavailable', '本机续编暂不可用，请保留填写内容后重试')
    if not user_id:
        return refusal(403, 'draft_identity_required', '请重新登录后恢复本机续编')
    try:
        if method == 'GET':
            value = store.get(user_id, draft_type)
        elif not isinstance(body, dict):
            raise ValueError('object required')
        elif method == 'PUT':
            value = store.put(user_id, draft_type, body.get('payload'), body.get('expected_revision'))
        else:
            value = store.clear(user_id, draft_type, body.get('expected_revision'))
        return 200, {'data': value}
    except DraftConflict:
        return refusal(409, 'draft_revision_conflict', '本机输入已有更新，请保留当前填写并重新读取核对')
    except (ValueError, RecursionError):
        return refusal(400, 'draft_input_invalid', '续编输入、身份或修订无效')
    except SQLAlchemyError:
        logger.error('publication draft storage unavailable')
        return refusal(503, 'draft_unavailable', '本机保存失败，请保留填写内容后重试')
