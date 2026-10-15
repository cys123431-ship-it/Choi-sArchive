#!/usr/bin/env python3
"""Index public, owned repositories by their actual GitHub creation date."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

OWNER = os.environ.get('ARCHIVE_OWNER', 'cys123431-ship-it')
REPOSITORY = os.environ.get('GITHUB_REPOSITORY', f'{OWNER}/Choi-sArchive')
KST = dt.timezone(dt.timedelta(hours=9))
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June',
          'July', 'August', 'September', 'October', 'November', 'December')
STAMP = re.compile(r'^마지막 목록 갱신: (.+)$', re.MULTILINE)
MANAGED = re.compile(r'archive/(?:README\.md|\d{4}/README\.md|'
                     r'\d{4}/\d{2}-[A-Za-z]+/README\.md)\Z')


def get_json(path):
    """Fail closed; never log credentials or untrusted API response bodies."""
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Choi-sArchive',
               'X-GitHub-Api-Version': '2022-11-28'}
    token = os.environ.get('GH_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(f'https://api.github.com{path}', headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if (error.code == 429 or 500 <= error.code < 600) and attempt < 2:
                print(f'GitHub API HTTP {error.code}; retrying', file=sys.stderr)
                time.sleep(2 ** (attempt + 1))
                continue
            remaining = error.headers.get('X-RateLimit-Remaining', 'unknown')
            raise RuntimeError(f'GitHub API HTTP {error.code} '
                               f'(rate limit remaining: {remaining})') from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise RuntimeError('GitHub API network/JSON error') from None


def public_count():
    profile = get_json(f'/users/{urllib.parse.quote(OWNER, safe="")}')
    if not isinstance(profile, dict) or profile.get('login', '').lower() != OWNER.lower():
        raise ValueError('Invalid GitHub user response')
    count = profile.get('public_repos')
    if type(count) is not int or count < 1:
        raise ValueError('Invalid public repository count; refusing replacement')
    return count


def collect_repositories():
    """Fetch every page and validate completeness before touching files."""
    count_before = public_count()
    repositories = {}
    page = 1
    while True:
        query = urllib.parse.urlencode({'type': 'owner', 'sort': 'full_name',
                                        'direction': 'asc', 'per_page': 100, 'page': page})
        batch = get_json(f'/users/{urllib.parse.quote(OWNER, safe="")}/repos?{query}')
        if not isinstance(batch, list) or len(batch) > 100:
            raise ValueError(f'Invalid repository response on page {page}')
        new_ids = 0
        for repo in batch:
            if not isinstance(repo, dict) or type(repo.get('private')) is not bool:
                raise ValueError(f'Invalid repository visibility on page {page}')
            # Intentionally use /users/{owner}/repos, never the private /user/repos.
            if repo['private'] or repo.get('visibility', 'public') != 'public':
                continue
            if repo.get('owner', {}).get('login', '').lower() != OWNER.lower():
                raise ValueError('Unexpected repository owner')
            repo_id, name = repo.get('id'), repo.get('name', '')
            if type(repo_id) is not int or not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
                raise ValueError('Invalid repository identity')
            full_name = f'{OWNER}/{name}'
            if repo.get('full_name', '').lower() != full_name.lower():
                raise ValueError('Inconsistent repository identity')
            url = repo.get('html_url')
            if not isinstance(url, str) or url.lower() != f'https://github.com/{full_name}'.lower():
                raise ValueError('Unexpected repository URL')
            created = dt.datetime.fromisoformat(repo['created_at'].replace('Z', '+00:00'))
            if created.tzinfo is None or created.utcoffset() != dt.timedelta(0):
                raise ValueError('Expected UTC created_at timestamp')
            entry = (created.astimezone(KST), name, url)
            if repo_id in repositories and repositories[repo_id] != entry:
                raise ValueError('Conflicting duplicate repository data')
            new_ids += repo_id not in repositories
            repositories[repo_id] = entry
        if len(batch) < 100:
            break
        if not new_ids:
            raise ValueError('Pagination made no progress')
        page += 1
    count_after = public_count()
    if count_before != count_after or len(repositories) != count_after:
        raise ValueError('Public repository count changed or pagination incomplete; retry later')
    if not any(f'{OWNER}/{row[1]}'.lower() == REPOSITORY.lower()
               for row in repositories.values()):
        raise ValueError('Archive missing from public response; refusing replacement')
    entries = [row for row in repositories.values()
               if f'{OWNER}/{row[1]}'.lower() != REPOSITORY.lower()]
    if len({name.lower() for _, name, _ in entries}) != len(entries):
        raise ValueError('Duplicate repository names')
    return entries, len(repositories), page


def render(entries, stamp):
    groups = defaultdict(list)
    for created, name, url in entries:
        groups[(created.year, created.month)].append((created, name, url))
    years = sorted({year for year, _ in groups}, reverse=True)
    repo_url = f'https://github.com/{REPOSITORY}'
    workflow_url = f'{repo_url}/actions/workflows/update-archive.yml'
    main = ["# Choi's Archive", '',
            f'공개 저장소 **{len(entries)}개** · GitHub 생성일 기준 · 한국 시간(KST)', '',
            f'마지막 목록 갱신: {stamp}', '',
            f'[![Update repository archive]({repo_url}/actions/workflows/update-archive.yml/badge.svg?branch=main)]({workflow_url})', '',
            f'매일 00:17(KST) 예약 갱신 · [수동 실행 및 실행 기록]({workflow_url})', '',
            'GitHub의 예약 실행은 지연될 수 있습니다. 새 공개 저장소와 삭제·공개 상태 변경은 다음 갱신에 반영됩니다.', '',
            '[전체 아카이브](archive/README.md)', '', '## 연도별 목록', '']
    index = ['# 저장소 아카이브', '',
             f'공개 저장소 {len(entries)}개 · 생성일 기준 · 한국 시간(KST)', '',
             '[홈](../README.md)', '']
    files = {}
    for year in years:
        total = sum(len(rows) for (y, _), rows in groups.items() if y == year)
        main.append(f'- [{year}년 · {total}개](archive/{year}/README.md)')
        index.extend([f'## [{year}년 · {total}개]({year}/README.md)', ''])
        year_lines = [f'# {year}년', '', f'공개 저장소 {total}개', '',
                      '[홈](../../README.md) · [전체 목록](../README.md)', '']
        for month in sorted((m for y, m in groups if y == year), reverse=True):
            dirname = f'{month:02d}-{MONTHS[month - 1]}'
            rows = sorted(groups[(year, month)], key=lambda row: (row[0], row[1].lower()))
            lines = [f'# {year}년 {month}월', '', f'공개 저장소 {len(rows)}개', '',
                     '[홈](../../../README.md) · [연도 목록](../README.md) · [전체 목록](../../README.md)', '',
                     '| 생성일 (KST, UTC+09:00) | 저장소 |', '| --- | --- |']
            for created, name, url in rows:
                lines.append(f'| {created:%Y-%m-%d %H:%M:%S} | [{name}]({url}) |')
            files[f'archive/{year}/{dirname}/README.md'] = '\n'.join(lines) + '\n'
            label = f'{month:02d}월 · {len(rows)}개'
            year_lines.append(f'- [{label}]({dirname}/README.md)')
            index.append(f'- [{label}]({year}/{dirname}/README.md)')
        index.append('')
        files[f'archive/{year}/README.md'] = '\n'.join(year_lines) + '\n'
    main.extend(['', '## 월별 바로가기', ''])
    for year in years:
        links = []
        for y, month in sorted(groups, reverse=True):
            if y == year:
                dirname = f'{month:02d}-{MONTHS[month - 1]}'
                links.append(f'[{month:02d}월](archive/{year}/{dirname}/README.md)')
        main.append(f'- {year}년: ' + ' · '.join(links))
    if not entries:
        main.append('아카이브 외에 공개 저장소가 없습니다.')
        index.append('아카이브 외에 공개 저장소가 없습니다.')
    main.extend(['', '기준: GitHub REST API의 `created_at`을 KST로 변환합니다. 비공개 저장소와 이 아카이브는 제외합니다.', '',
                 '목록이 바뀔 때만 갱신 시각과 파일을 커밋합니다. 매회 조회 결과는 Actions 실행 기록에서 확인할 수 있습니다.'])
    files['README.md'] = '\n'.join(main) + '\n'
    files['archive/README.md'] = '\n'.join(index).rstrip() + '\n'
    return files


def update_files(root, entries):
    root = Path(root)
    readme = root / 'README.md'
    current = readme.read_text(encoding='utf-8') if readme.exists() else ''
    match = STAMP.search(current)
    files = render(entries, match.group(1) if match else '')
    existing = {p.relative_to(root).as_posix() for p in (root / 'archive').rglob('README.md')
                if MANAGED.fullmatch(p.relative_to(root).as_posix())}
    stale = existing - files.keys()
    changed = bool(stale) or any(not (root / path).exists() or
                               (root / path).read_text(encoding='utf-8') != content
                               for path, content in files.items())
    if not changed:
        return False
    stamp = dt.datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')
    files = render(entries, stamp)
    # Stage all output before replacing managed files. Never remove the whole archive.
    with tempfile.TemporaryDirectory(prefix='archive-build-') as stage:
        for path, content in files.items():
            source = Path(stage) / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(content, encoding='utf-8')
        for path in files:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write((Path(stage) / path).read_bytes())
            try:
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        for path in stale:
            (root / path).unlink()
        for directory in sorted((root / 'archive').rglob('*'), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
    return True


def main():
    try:
        entries, collected, pages = collect_repositories()
        changed = update_files(Path(__file__).resolve().parents[2], entries)
        months = len({(created.year, created.month) for created, _, _ in entries})
        message = (f'Public repositories fetched: {collected}; pages: {pages}; '
                   f'archived: {len(entries)}; months: {months}; changed: {str(changed).lower()}')
        print(message)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as handle:
                handle.write(f'### Archive generation\n\n{message}\n\n'
                             f'Checked: {dt.datetime.now(KST):%Y-%m-%d %H:%M:%S} KST\n')
        return 0
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as error:
        print(f'Archive generation failed: {error}. Generation stopped; no commit.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
