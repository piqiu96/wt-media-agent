"""Identify the two observed cover exports without retaining CSRF or images."""
import base64
from email import policy
from email.parser import BytesParser


def cover_upload_kind(content_type, body):
    if (not isinstance(content_type, str) or '\r' in content_type or '\n' in content_type
            or not content_type.lower().startswith('multipart/form-data;')
            or not isinstance(body, bytes) or len(body) > 30 * 1024 * 1024):
        raise ValueError('invalid cover export')
    message = BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
    parts = list(message.iter_parts())
    images = [p for p in parts if p.get_param('name', header='content-disposition') == 'cover']
    if len(images) != 1 or message.defects or images[0].defects:
        raise ValueError('missing unique cover export')
    value = images[0].get_payload(decode=True)
    prefix, separator, encoded = value.partition(b',')
    if separator != b',' or prefix not in {b'data:image/png;base64', b'data:image/jpeg;base64'}:
        raise ValueError('unsupported cover export')
    image = base64.b64decode(encoded, validate=True)
    if prefix == b'data:image/png;base64' and image.startswith(b'\x89PNG\r\n\x1a\n') and len(image) >= 24:
        width, height = int.from_bytes(image[16:20], 'big'), int.from_bytes(image[20:24], 'big')
    elif prefix == b'data:image/jpeg;base64' and image.startswith(b'\xff\xd8'):
        width, height, offset = 0, 0, 2
        while offset + 4 <= len(image):
            if image[offset] != 255:
                break
            marker = image[offset + 1]
            length = int.from_bytes(image[offset + 2:offset + 4], 'big')
            if length < 2 or offset + 2 + length > len(image):
                break
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf} and length >= 8:
                height = int.from_bytes(image[offset + 5:offset + 7], 'big')
                width = int.from_bytes(image[offset + 7:offset + 9], 'big')
                break
            offset += 2 + length
    else:
        raise ValueError('invalid cover export bytes')
    if width <= 0 or height <= 0:
        raise ValueError('missing cover dimensions')
    # The platform rounds cropped dimensions to whole pixels.
    if abs(width * 3 - height * 4) <= 4:
        return 'cover43'
    if abs(width * 9 - height * 16) <= 16:
        return 'cover'
    raise ValueError('unexpected cover export ratio')
