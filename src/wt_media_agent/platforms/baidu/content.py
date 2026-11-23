"""Confirmed Baidu content formatting shared by publishing and public checks."""
from wt_media_agent.platforms.publish import PublicationInputError


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
