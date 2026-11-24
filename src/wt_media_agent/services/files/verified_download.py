"""Download immutable resources through the basic client and verify local bytes."""
import hashlib
import math
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from wt_media_agent.clients.transfer.source import open_source


class ResourceVerificationError(RuntimeError):
    """Invalid input or bytes; signed addresses must never appear in errors."""


def download_verified(grant: dict, target: Path, *, current=lambda: True, deadline=None):
    """The grant remains in memory. Size and digest are checked from actual bytes."""
    raw=grant.get('url'); size=grant.get('size'); digest=grant.get('sha256')
    u=urlsplit(raw) if isinstance(raw,str) else None
    if u is None or u.scheme!='https' or not u.hostname or u.username or u.password or type(size) is not int or size<=0 or not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise ResourceVerificationError('invalid single-object download grant')
    deadline = time.monotonic() + 900.0 if deadline is None else deadline
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ResourceVerificationError('invalid resource deadline')
    def check_current():
        if not current() or time.monotonic() >= deadline:
            raise ResourceVerificationError('resource preparation stopped or exceeded deadline')
    check_current()
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    part=target.with_suffix(target.suffix+'.part'); count=0; sha=hashlib.sha256()
    try:
        with closing(open_source(raw, timeout=min(30.0, max(.001, deadline-time.monotonic())))) as response, part.open('wb') as stream:
            part.chmod(0o600)
            while True:
                check_current()
                chunk=getattr(response, 'read_available', response.read)(128*1024)
                check_current()
                if not chunk: break
                count+=len(chunk)
                if count>size: raise ResourceVerificationError('resource exceeds frozen size')
                sha.update(chunk);stream.write(chunk)
        if count!=size or sha.hexdigest()!=digest: raise ResourceVerificationError('resource digest or size differs')
        check_current()
        part.replace(target)
    except Exception:
        part.unlink(missing_ok=True)
        # Never surface a signed address, including urllib's redirect errors.
        raise ResourceVerificationError('resource download or verification failed') from None
