"""Baijiahao (Baidu) platform identity client."""

from wt_media_agent.clients.baijiahao.identity import (
    AVATAR_BASE_URL,
    LOGININFO_URL_TEMPLATE,
    identify_baijiahao,
)

__all__ = ["AVATAR_BASE_URL", "LOGININFO_URL_TEMPLATE", "identify_baijiahao"]
