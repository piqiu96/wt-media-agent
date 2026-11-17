"""Verified publication resources and live browser identity, with no submit operation."""
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from wt_media_agent.services.browser.cdp import read_live_cookies
from wt_media_agent.clients.baidu.identity import identify_baidu
class PublicationInputError(RuntimeError): pass

def download_verified(grant: dict, target: Path):
    """The grant remains in memory. Size and digest are checked from actual bytes."""
    raw=grant.get('url'); size=grant.get('size'); digest=grant.get('sha256')
    u=urlsplit(raw) if isinstance(raw,str) else None
    if u is None or u.scheme!='https' or not u.hostname or u.username or u.password or type(size) is not int or size<=0 or not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise PublicationInputError('invalid single-object download grant')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    part=target.with_suffix(target.suffix+'.part'); count=0; sha=hashlib.sha256()
    try:
        with urlopen(Request(raw),timeout=30) as response, part.open('wb') as stream:
            part.chmod(0o600)
            while True:
                chunk=response.read(128*1024)
                if not chunk: break
                count+=len(chunk)
                if count>size: raise PublicationInputError('resource exceeds frozen size')
                sha.update(chunk);stream.write(chunk)
        if count!=size or sha.hexdigest()!=digest: raise PublicationInputError('resource digest or size differs')
        part.replace(target)
    except Exception:
        part.unlink(missing_ok=True)
        # Never surface a signed address, including urllib's redirect errors.
        raise PublicationInputError('resource download or verification failed') from None


def live_baidu_identity(bitbrowser, profile_id):
    endpoint=bitbrowser.open_profile_with_devtools(profile_id)
    return endpoint,identify_baidu(read_live_cookies(endpoint))
