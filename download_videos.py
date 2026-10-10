"""Local video-only entry point. Does not import scheduler, ASR or email code."""

import argparse
import concurrent.futures
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

from src.api.icourse import ICourseClient
from src.api.webvpn import WebVPNSession, get_vpn_url
from src.runtime import config


class DownloadError(RuntimeError):
    pass


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.new')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def curl_config(values):
    # curl configuration quoting keeps authentication out of process arguments.
    return '\n'.join(f'{key} = {json.dumps(str(value), ensure_ascii=False)}'
                     for key, value in values) + '\n'


class CurlResponse:
    def __init__(self, status, content):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code != 200:
            raise DownloadError(f'课程接口 HTTP {self.status_code}，请检查网络或重新登录。')

    def json(self):
        try:
            return json.loads(self.content)
        except ValueError:
            raise DownloadError('课程接口未返回有效 JSON，登录可能已过期。') from None


class DirectSession:
    """Reuse one iCourse session; curl uses Windows TLS and avoids Clash by default."""
    def __init__(self, cookie, proxy):
        if '\r' in cookie or '\n' in cookie or not cookie:
            raise DownloadError('Cookie 文件格式错误。')
        self.cookie, self.proxy = cookie, proxy

    def get(self, url, params=None, timeout=60):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'icourse.fudan.edu.cn':
            raise DownloadError('课程接口必须位于 icourse.fudan.edu.cn。')
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        command = curl_command(self.proxy, timeout)
        command += ['--write-out', '\n%{http_code}']
        values = [('url', url), ('header', 'Cookie: ' + self.cookie),
                  ('header', 'User-Agent: ' + config.USER_AGENT)]
        for attempt in range(3):
            result = subprocess.run(command, input=curl_config(values).encode('utf-8'),
                                    capture_output=True)
            if result.returncode not in (7, 28, 35, 56):
                break
            if attempt < 2:
                time.sleep(2 ** attempt)
        if result.returncode:
            raise DownloadError(f'课程接口连接失败（curl {result.returncode}）。')
        content, status = result.stdout.rsplit(b'\n', 1)
        return CurlResponse(int(status), content)


def curl_command(proxy, timeout):
    command = ['curl.exe' if os.name == 'nt' else 'curl', '--config', '-',
               '--silent', '--show-error', '--connect-timeout', '15',
               '--max-time', str(timeout)]
    command += ['--proxy', proxy] if proxy else ['--noproxy', '*']
    return command


def authenticate(args):
    cache = args.session_dir / 'session.json'
    if args.cookie_file:
        raw = args.cookie_file.read_text(encoding='utf-8-sig').strip()
        cookie = raw.removeprefix('Cookie:').removeprefix('cookie:').strip()
        session = DirectSession(cookie, args.proxy)
        client = ICourseClient(session)
        client.get_userinfo()  # Validate before saving the credential.
        write_json(cache, {'mode': 'direct', 'cookie': cookie})
        return client, session
    saved = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else None
    if not args.login and saved and saved['mode'] == 'direct':
        session = DirectSession(saved['cookie'], args.proxy)
        client = ICourseClient(session)
        client.get_userinfo()
        return client, session
    session = WebVPNSession()
    session.session.trust_env = False
    if args.proxy:
        session.session.proxies.update({'http': args.proxy, 'https': args.proxy})
    if not args.login and saved and saved['mode'] == 'webvpn':
        for cookie in saved['cookies']:
            session.session.cookies.set(**cookie)
    else:
        if not args.login:
            raise DownloadError('尚未登录。首次使用加 --login，或用 --cookie-file 导入一次已有会话。')
        student_id = os.getenv('StuId') or input('UIS 学号：').strip()
        password = os.getenv('UISPsw') or getpass.getpass('UIS 密码（不显示、不保存）：')
        if not student_id or not password:
            raise DownloadError('学号和密码不能为空。')
        if not session.login(student_id, password):
            raise DownloadError('WebVPN 登录失败；请确认凭据或是否需要验证码。')
        session.authenticate_icourse(student_id, password)
        password = None
    client = ICourseClient(session)
    client.get_userinfo()
    write_json(cache, {'mode': 'webvpn', 'cookies': [
        {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path,
         'secure': c.secure, 'expires': c.expires} for c in session.session.cookies]})
    return client, session


def available_lectures(client, course_id):
    # Use the same course discovery as upstream and also honor visibility/watch rules.
    response = client.vpn.get(client.base_url + '/courseapi/v3/multi-search/get-course-detail',
                              params={'course_id': course_id})
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0:
        raise DownloadError('无法读取课程目录，请重新登录。')
    course = payload['data']
    result = []
    for months in course.get('sub_list', {}).values():
        for days in months.values():
            for items in days.values():
                for item in items:
                    if (str(item.get('playback_status')) == '1'
                            and item.get('can_watch') is not False
                            and item.get('show') != 'no'
                            and str(item.get('sub_delete', '0')) != '1'):
                        result.append({'sub_id': str(item['id']),
                                       'sub_title': item.get('sub_title', '')})
    return course['title'], sorted(result, key=lambda x: (x['sub_title'], x['sub_id']))


def video_info(client, course_id, sub_id):
    response = client.vpn.get(client.base_url + '/courseapi/v3/portal-home-setting/get-sub-info',
                              params={'course_id': course_id, 'sub_id': sub_id})
    response.raise_for_status()
    payload = response.json()
    # Only released playback URLs. Never fall back to review-gated nested URLs.
    if payload.get('code') != 0:
        raise DownloadError(f'课次 {sub_id} 尚未开放或会话无效，已停止该课次。')
    info = payload.get('data') or {}
    candidates = info.get('video_list') or {}
    candidates = candidates.values() if isinstance(candidates, dict) else candidates
    videos = [v for v in candidates if isinstance(v, dict)
              and urlparse(v.get('preview_url') or '').path.endswith('.mp4')]
    # type 3 is the classroom/blackboard view, matching the user's first download.
    videos.sort(key=lambda v: str(v.get('type')) != '3')
    if not videos:
        raise DownloadError(f'课次 {sub_id} 没有已开放的 MP4 录屏。')
    video = videos[0]
    parsed = urlparse(video['preview_url'])
    if parsed.scheme != 'https' or parsed.hostname != 'icourse.fudan.edu.cn':
        raise DownloadError('视频地址不属于预期的课程服务器。')
    base = urlunparse(parsed._replace(query='', fragment=''))
    server_now = int(info.get('now') or time.time())
    return {'base': base, 'offset': server_now - int(time.time()),
            'duration': float(video.get('duration') or 0)}


def parse_headers(path):
    blocks = path.read_text(encoding='iso-8859-1').strip().split('\n\n')
    # Ignore proxy CONNECT/100 responses; examine the last HTTP response.
    block = next((b for b in reversed(blocks) if b.startswith('HTTP/')), '')
    lines = block.splitlines()
    if not lines:
        return 0, {}
    return int(lines[0].split()[1]), {k.lower().strip(): v.strip()
        for k, v in (line.split(':', 1) for line in lines[1:] if ':' in line)}


class MediaTransfer:
    def __init__(self, client, session, info, args):
        self.client, self.session, self.info, self.args = client, session, info, args
        self.stop = threading.Event()

    def fetch(self, start, end, target, expected=None):
        last_status = None
        for attempt in range(6):
            if self.stop.is_set():
                raise DownloadError('下载已停止。')
            signed = self.client.sign_video_url(self.info['base'],
                        now=int(time.time()) + self.info['offset'])
            if isinstance(self.session, DirectSession):
                url, cookie = signed, self.session.cookie
            else:
                import requests
                url = get_vpn_url(signed)
                prepared = self.session.session.prepare_request(requests.Request('GET', url))
                cookie = prepared.headers.get('Cookie', '')
            with tempfile.TemporaryDirectory(dir=self.args.session_dir) as temporary:
                headers = Path(temporary) / 'response.headers'
                values = [('url', url), ('output', target), ('dump-header', headers),
                          ('range', f'{start}-{end}'), ('header', 'Cookie: ' + cookie),
                          ('header', 'User-Agent: ' + config.USER_AGENT),
                          ('header', 'Accept-Encoding: identity'),
                          ('max-filesize', end - start + 1)]
                command = curl_command(self.args.proxy, 240) + ['--fail']
                result = subprocess.run(command, input=curl_config(values).encode('utf-8'),
                                        capture_output=True)
                if headers.exists():
                    status, fields = parse_headers(headers)
                    last_status = status
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', fields.get('content-range', ''))
                    if status == 401:
                        raise DownloadError('视频鉴权失败，可能需要 --login 刷新会话。')
                    if result.returncode == 0:
                        if status != 206 or not match or tuple(map(int, match.groups()[:2])) != (start, end):
                            raise DownloadError('服务器未返回请求的完整视频分段。')
                        meta = {'bytes': int(match[3]), 'etag': fields.get('etag', '')}
                        if expected and meta != expected:
                            raise DownloadError('视频版本发生变化，请重新列出课次后再运行。')
                        if target.stat().st_size != end - start + 1:
                            raise DownloadError('视频分段长度不完整。')
                        return meta
            if attempt < 5:
                time.sleep(min(2 ** (attempt + 1), 20))
        if last_status == 403:
            if not self.client.check_alive():
                raise DownloadError('登录会话已失效，请用 --login 重新登录后续传。')
            raise DownloadError('登录仍有效，但视频服务器持续拒绝该分段；已保留分段，请稍后重试。')
        raise DownloadError('视频分段下载失败；已完成的分段保留，重跑可续传。')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_video(path, expected, duration, ffprobe, ffmpeg=None):
    if not path.exists() or path.stat().st_size != expected:
        raise DownloadError('文件大小与服务器不一致。')
    command = [ffprobe, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]
    result = subprocess.run(command, capture_output=True, timeout=60)
    if result.returncode:
        raise DownloadError('视频结构检查失败。')
    data = json.loads(result.stdout)
    streams = data.get('streams', [])
    seconds = float(data.get('format', {}).get('duration', 0))
    if not any(s.get('codec_type') == 'video' for s in streams) or seconds <= 0:
        raise DownloadError('文件中没有有效视频轨。')
    if duration and abs(seconds - duration) > 10:
        raise DownloadError('视频时长与课程接口不一致。')
    if ffmpeg:
        for position in (0, seconds / 2, max(0, seconds - 5)):
            command = [ffmpeg, '-nostdin', '-v', 'error', '-ss', str(position), '-i', str(path),
                       '-t', '2', '-map', '0:v:0', '-map', '0:a:0?', '-vsync', '0',
                       '-enc_time_base:v', '1:90000', '-f', 'null', os.devnull]
            result = subprocess.run(command, capture_output=True, timeout=60)
            if result.returncode or result.stderr.strip():
                raise DownloadError('视频开头、中间或结尾的解码检查失败。')
    return round(seconds, 3)


def download_one(transfer, target, meta, cache, ffprobe, ffmpeg):
    if not meta['etag']:
        raise DownloadError('服务器未提供 ETag，不能安全复用分段缓存。')
    identity = hashlib.sha256((transfer.info['base'] + meta['etag']).encode()).hexdigest()[:20]
    folder = cache / identity
    folder.mkdir(parents=True, exist_ok=True)
    chunk_size = 32 * 1024 * 1024
    count = (meta['bytes'] + chunk_size - 1) // chunk_size
    cached = 0
    for index in range(count):
        chunk = folder / f'{index:04d}.part'
        if chunk.exists():
            cached += chunk.stat().st_size
    required = meta['bytes'] * 2 - min(cached, meta['bytes']) + 256 * 1024 * 1024
    if shutil.disk_usage(target.parent).free < required:
        raise DownloadError('目标磁盘空间不足（需要容纳分段与合并文件）。')

    def worker(index):
        start, end = index * chunk_size, min((index + 1) * chunk_size, meta['bytes']) - 1
        chunk = folder / f'{index:04d}.part'
        checksum = folder / f'{index:04d}.json'
        if chunk.exists() and checksum.exists() and chunk.stat().st_size == end - start + 1:
            saved = json.loads(checksum.read_text(encoding='utf-8'))
            if saved.get('sha256') == digest(chunk):
                return chunk
        incoming = chunk.with_suffix('.incoming')
        transfer.fetch(start, end, incoming, meta)
        write_json(checksum, {'sha256': digest(incoming)})
        os.replace(incoming, chunk)
        return chunk

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=transfer.args.workers) as pool:
        futures = [pool.submit(worker, i) for i in range(count)]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
                completed += 1
                print(f'  分段 {completed}/{count} 完成', flush=True)
        except BaseException:
            transfer.stop.set()
            for future in futures:
                future.cancel()
            raise
    assembly = target.with_suffix('.mp4.assembling')
    if target.exists():
        raise DownloadError('目标文件已存在且未通过检查；请手动检查或改名后重试。')
    with assembly.open('wb') as output:
        for index in range(count):
            with (folder / f'{index:04d}.part').open('rb') as chunk:
                shutil.copyfileobj(chunk, output, 1024 * 1024)
    seconds = validate_video(assembly, meta['bytes'], transfer.info['duration'], ffprobe, ffmpeg)
    if target.exists():
        raise DownloadError('目标文件已存在，停止覆盖。')
    assembly.rename(target)
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description='仅下载 iCourse 已开放的课堂录屏，不转写、不发邮件。')
    parser.add_argument('--course-id', default='39173')
    parser.add_argument('--output', type=Path, default=Path('D:/desktop/26秋/课堂录制'))
    parser.add_argument('--session-dir', type=Path, default=Path(__file__).parent / '.local-downloads')
    parser.add_argument('--cookie-file', type=Path, help='首次导入本机 Cookie 或 Cookie: 请求头文件')
    parser.add_argument('--login', action='store_true', help='交互式 UIS/WebVPN 登录，密码不保存')
    parser.add_argument('--download', action='store_true', help='真正下载；默认只列出并探测')
    parser.add_argument('--sub-ids', nargs='+', help='只处理这些课次 ID；不填则处理所有可回放课次')
    parser.add_argument('--proxy', default='', help='默认直连；可显式指定代理地址')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 17))
    parser.add_argument('--ffprobe', default=shutil.which('ffprobe'))
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'), help='可选：抽查头、中、尾解码')
    args = parser.parse_args(argv)
    args.session_dir.mkdir(parents=True, exist_ok=True)
    if not shutil.which('curl.exe' if os.name == 'nt' else 'curl'):
        raise DownloadError('请先安装 curl。Windows 10/11 通常自带。')
    if args.download and not args.ffprobe:
        raise DownloadError('下载前请安装 ffprobe，或用 --ffprobe 指定它的路径。')
    client, session = authenticate(args)
    title, lectures = available_lectures(client, args.course_id)
    if args.sub_ids:
        available = {l['sub_id'] for l in lectures}
        if set(args.sub_ids) - available:
            raise DownloadError('指定的课次不在当前已开放的回放列表中。')
        lectures = [l for l in lectures if l['sub_id'] in args.sub_ids]
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    print(f'{title}：找到 {len(lectures)} 节可回放课堂。', flush=True)
    for lecture in lectures:
        print(lecture['sub_title'], flush=True)
        info = video_info(client, args.course_id, lecture['sub_id'])
        transfer = MediaTransfer(client, session, info, args)
        with tempfile.TemporaryDirectory(dir=args.session_dir) as scratch:
            meta = transfer.fetch(0, 1023, Path(scratch) / 'probe.bin')
        subtitle = re.sub(r'^(\d{4}-\d{2}-\d{2})(第)', r'\1_\2', lecture['sub_title'])
        resolution = '1080p' if '1920_1080' in urlparse(info['base']).path else '原画'
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', f'{title}_{subtitle}_{resolution}').rstrip('. ')
        target = args.output / (name + '.mp4')
        status, seconds = '待下载', None
        if target.exists():
            if not args.ffprobe:
                status = '文件已存在，尚需 ffprobe 检查'
            else:
                seconds = validate_video(target, meta['bytes'], info['duration'], args.ffprobe, args.ffmpeg)
                status = '已验证，跳过'
        elif args.download:
            seconds = download_one(transfer, target, meta, args.output / '.icourse-downloads',
                                   args.ffprobe, args.ffmpeg)
            status = '下载并验证完成'
        print(f'  {meta["bytes"] / 1024**3:.2f} GiB · {status}', flush=True)
        records.append({**lecture, 'path': str(target), 'bytes': meta['bytes'],
                        'etag': meta['etag'], 'duration': seconds, 'status': status})
        write_json(args.session_dir / f'course-{args.course_id}-manifest.json',
                   {'course_id': args.course_id, 'title': title, 'lectures': records})
    print('本次完成。' if args.download else '探测完成；加 --download 才会下载其余视频。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已中止；完成的分段保留。', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        # Exception messages from authentication/network libraries can contain secrets.
        message = str(error) if type(error) is DownloadError else f'{type(error).__name__}：操作失败，请检查登录或网络。'
        print(message, file=sys.stderr)
        sys.exit(1)
