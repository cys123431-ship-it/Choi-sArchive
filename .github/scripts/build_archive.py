#!/usr/bin/env python3
"""Build a public repository archive from GitHub's actual created_at timestamps."""
import datetime as dt
import json
import os
import pathlib
import shutil
import urllib.parse
import urllib.request
from collections import defaultdict

OWNER = os.environ.get("ARCHIVE_OWNER", "cys123431-ship-it")
ROOT = pathlib.Path("archive")
TOKEN = os.environ.get("GH_TOKEN", "")
KST = dt.timezone(dt.timedelta(hours=9))
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

def get_page(page):
    url = f"https://api.github.com/users/{urllib.parse.quote(OWNER)}/repos?per_page=100&page={page}&type=owner"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Choi-sArchive"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.load(response)

repos = []
for page in range(1, 100):
    batch = get_page(page)
    repos.extend(batch)
    if len(batch) < 100:
        break
else:
    raise RuntimeError("Repository pagination limit reached")

groups = defaultdict(list)
for repo in repos:
    if repo.get("private") or repo["name"] == "Choi-sArchive":
        continue
    created = dt.datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00")).astimezone(KST)
    groups[(created.year, created.month)].append((created, repo["name"], repo["html_url"]))

if not groups:
    raise RuntimeError("No public repositories found; refusing to clear archive")

if ROOT.exists():
    shutil.rmtree(ROOT)
ROOT.mkdir()
years = sorted({year for year, _ in groups}, reverse=True)
index = ["# 저장소 아카이브", "", "GitHub 생성일 기준 · 한국 시간(KST) · 공개 저장소만 표시", ""]
for year in years:
    year_dir = ROOT / str(year)
    year_dir.mkdir()
    year_lines = [f"# {year}년", "", "[전체 목록](../README.md)", ""]
    for month in sorted((m for y, m in groups if y == year), reverse=True):
        key = (year, month)
        dirname = f"{month:02d}-{MONTHS[month-1]}"
        folder = year_dir / dirname
        folder.mkdir()
        entries = sorted(groups[key], key=lambda row: (row[0], row[1].lower()))
        lines = [f"# {year}년 {month}월", "", "[연도별 목록](../README.md) · [전체 목록](../../README.md)", "",
                 "| 생성일 (KST) | 저장소 |", "| --- | --- |"]
        for created, name, url in entries:
            lines.append(f"| {created:%Y-%m-%d %H:%M} | [{name}]({url}) |")
        (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        label = f"{month:02d}월 — {len(entries)}개"
        year_lines.append(f"- [{label}]({dirname}/README.md)")
        index.append(f"- [{year}년 {label}]({year}/{dirname}/README.md)")
    (year_dir / "README.md").write_text("\n".join(year_lines) + "\n", encoding="utf-8")
(ROOT / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
print(f"Archived {sum(map(len, groups.values()))} public repositories across {len(groups)} months")
