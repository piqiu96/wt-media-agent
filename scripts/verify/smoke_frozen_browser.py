#!/usr/bin/env python3
"""Exercise the Playwright driver shipped in a frozen Sidecar, without posting.

The optional browser check only opens a network-blocked fixture tab. It proves
bundled driver/path handling, not the Sidecar task loop or real-platform success.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from PyInstaller.archive.readers import CArchiveReader


def probe(binary, endpoint=None, file=None):
    archive = CArchiveReader(str(binary))
    prefix = 'playwright/driver/'
    entries = [name for name in archive.toc if name.startswith(prefix)]
    if not entries:
        raise RuntimeError('Sidecar has no bundled Playwright driver')
    with tempfile.TemporaryDirectory(prefix='wt-frozen-playwright-') as folder:
        root = Path(folder)
        for name in entries:
            relative = Path(name).relative_to(prefix)
            if '..' in relative.parts:
                raise ValueError('invalid archive path')
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.extract(name))
        node = root / ('node.exe' if os.name == 'nt' else 'node')
        node.chmod(0o700)
        version = subprocess.check_output([str(node), str(root/'package/cli.js'), '--version'], text=True).strip()
        print(json.dumps({'bundled_driver': version, 'files': len(entries)}), flush=True)
        if endpoint and file:
            script = """
const {chromium}=require(process.argv[1]);
(async()=>{const b=await chromium.connectOverCDP('http://'+process.argv[2],{isLocal:true});
 const p=await b.contexts()[0].newPage();
 try{await p.route('**/*',r=>r.request().url()==='https://local-file-test.invalid/'?r.fulfill({contentType:'text/html',body:'<input type="file">'}):r.abort());
 await p.goto('https://local-file-test.invalid/');await p.locator('input').setInputFiles(process.argv[3]);
 const size=await p.locator('input').evaluate(e=>e.files[0].size);
 if(size!==Number(process.argv[4]))throw Error('file size differs');
 console.log(JSON.stringify({bundled_browser_connection:true,file_bytes:size,platform_submit:false}));
 }finally{await p.close();await b.close()}})().catch(e=>{console.error(e.name);process.exitCode=1});
"""
            subprocess.run([str(node), '-e', script, str(root/'package/index.js'), endpoint,
                            str(file.resolve()), str(file.stat().st_size)], check=True, timeout=45)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--endpoint', help='explicit local BitBrowser host:port for an isolated fixture tab')
    parser.add_argument('--file', type=Path, help='local test file; never sent to a platform')
    args = parser.parse_args()
    if bool(args.endpoint) != bool(args.file):
        parser.error('--endpoint and --file go together')
    if args.endpoint:
        from wt_media_agent.services.browser.session import BrowserSession
        BrowserSession(args.endpoint)  # validate loopback endpoint before connecting
    probe(args.binary, args.endpoint, args.file)


if __name__ == '__main__':
    main()
