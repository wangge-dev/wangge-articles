#!/usr/bin/env python3
"""Publish saved WeChat article HTML and images; no account session is used."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import email
from email import policy
import html
import io
import json
from pathlib import Path
import re
import shutil
import urllib.parse
import urllib.request
import zipfile

from bs4 import BeautifulSoup, Comment
from PIL import Image

REPOSITORY = 'wangge-dev/wangge-articles'
RELEASE_TAG = 'html-archive'
DOWNLOAD_BASE = f'https://github.com/{REPOSITORY}/releases/download/{RELEASE_TAG}'
IMAGE_HOST = 'mmbiz.qpic.cn'


def normalized_url(value):
    value = html.unescape(value or '')
    parts = urllib.parse.urlsplit(value)
    if parts.hostname == IMAGE_HOST:
        return urllib.parse.urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))
    return value


def image_extension(data):
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
        return {'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif', 'WEBP': '.webp'}[image.format]


class ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname != IMAGE_HOST:
            raise ValueError('Image redirect leaves the declared image host')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_image(url):
    if urllib.parse.urlsplit(url).hostname != IMAGE_HOST:
        raise ValueError('Only public WeChat article image URLs are supported')
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://mp.weixin.qq.com/'})
    opener = urllib.request.build_opener(ImageRedirect())
    with opener.open(request, timeout=40) as response:
        data = response.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise ValueError('Article image exceeds 25 MiB')
    image_extension(data)
    return data


def source_file(root, article_id):
    candidates = [p for p in root.glob('*.html') if p.name.startswith(f'[{article_id}]')]
    candidates.sort(key=lambda p: (bool(re.search(r' \(\d+\)$', p.stem)), len(p.name), p.name))
    if not candidates:
        raise FileNotFoundError(f'Missing saved HTML: {article_id}')
    return candidates[0]


def write_zip(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, name in files:
            archive.write(source, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('--ids', nargs='*', help='Process these article IDs; default is the full metadata catalog')
    args = parser.parse_args()
    root = args.repo_root.resolve()
    work = (args.work_dir or root.parent / 'wangge-articles-downloads').resolve()
    work.mkdir(parents=True, exist_ok=True)
    cache = work / 'image-cache'
    cache.mkdir(exist_ok=True)
    packages = work / 'packages'
    packages.mkdir(exist_ok=True)
    records = json.loads((root / 'metadata/articles.json').read_text(encoding='utf-8-sig'))
    selected = [r for r in records if args.ids is None or r['id'] in args.ids]
    if args.ids is not None and set(args.ids) != {r['id'] for r in selected}:
        raise ValueError('Some requested article IDs do not exist in metadata')
    cache_map_path = work / 'image-cache.json'
    cache_map = json.loads(cache_map_path.read_text(encoding='utf-8')) if cache_map_path.exists() else {}
    documents = {}
    urls = set()
    recovered = 0
    for record in selected:
        path = source_file(args.source_root, record['id'])
        soup = BeautifulSoup(path.read_text(encoding='utf-8-sig'), 'html.parser')
        body = soup.select_one('#js_content')
        if body is None:
            raise ValueError(f"Saved HTML has no article body: {record['id']}")
        documents[record['id']] = (soup, body)
        urls.update(normalized_url(tag.get('data-src') or tag.get('src')) for tag in body.find_all('img'))
        mhtml = path.with_suffix('.mhtml')
        if mhtml.exists():
            message = email.message_from_bytes(mhtml.read_bytes(), policy=policy.default)
            for part in message.walk():
                url = normalized_url(part.get('Content-Location', ''))
                if url in urls and url not in cache_map and part.get_content_type().startswith('image/'):
                    data = part.get_payload(decode=True)
                    try:
                        extension = image_extension(data)
                    except Exception:
                        continue
                    name = f'{len(cache_map) + 1:05d}{extension}'
                    (cache / name).write_bytes(data)
                    cache_map[url] = name
                    recovered += 1
    missing = sorted(url for url in urls if url not in cache_map or not (cache / cache_map[url]).exists())
    print(f'Articles: {len(selected)}; image URLs: {len(urls)}; recovered from MHTML: {recovered}; fetch: {len(missing)}', flush=True)
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(fetch_image, url): url for url in missing}
        for number, future in enumerate(concurrent.futures.as_completed(jobs), 1):
            url = jobs[future]
            try:
                data = future.result()
                name = f'{len(cache_map) + 1:05d}{image_extension(data)}'
                (cache / name).write_bytes(data)
                cache_map[url] = name
            except Exception as error:
                failures.append({'url': url, 'error': type(error).__name__})
            if number % 50 == 0 or number == len(jobs):
                cache_map_path.write_text(json.dumps(cache_map, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
                print(f'Images processed: {number}/{len(jobs)}; failures: {len(failures)}', flush=True)
    cache_map_path.write_text(json.dumps(cache_map, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if failures:
        (work / 'image-failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
        raise RuntimeError(f'{len(failures)} article images unavailable; rerun to retry original URLs')

    summaries = []
    for record in selected:
        soup, body = documents[record['id']]
        original_text = body.get_text()
        folder = root / 'html' / record['date'][:4] / record['id']
        image_dir = folder / 'images'
        image_dir.mkdir(parents=True, exist_ok=True)
        replacements = {}
        image_files = []
        for number, tag in enumerate(body.find_all('img'), 1):
            url = normalized_url(tag.get('data-src') or tag.get('src'))
            cached = cache / cache_map[url]
            relative = f'images/{number:03d}{cached.suffix}'
            shutil.copyfile(cached, folder / relative)
            image_files.append(relative)
            replacements[url] = (folder / relative).relative_to(root).as_posix()
            tag['src'] = relative
            for attribute in ('data-src', 'srcset', 'data-original'):
                tag.attrs.pop(attribute, None)
            tag['loading'] = 'eager'
        for tag in list(body.find_all('script')):
            tag.decompose()
        for tag in list(body.find_all(['iframe', 'object', 'embed'])):
            tag.unwrap()
        for comment in body.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()
        for tag in body.find_all(True):
            for attribute in list(tag.attrs):
                if attribute.lower().startswith('on'):
                    del tag.attrs[attribute]
        body.attrs.pop('style', None)
        assert body.get_text() == original_text, f"Article text changed: {record['id']}"
        css = '\n'.join(tag.get_text() for tag in soup.find_all('style'))
        title = html.escape(record['title'])
        source = html.escape(record['source_url'], quote=True)
        output = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style>
<style>body{{background:#fff!important;color:#222}}.archive-page{{max-width:760px;margin:32px auto;padding:0 20px}}.archive-page h1{{font-size:26px;line-height:1.5}}.archive-meta{{color:#666;margin:16px 0 28px;line-height:1.8}}#js_content{{visibility:visible!important;opacity:1!important;overflow-wrap:anywhere}}#js_content img{{max-width:100%!important;height:auto!important}}.archive-note{{border-top:1px solid #ddd;margin-top:32px;padding-top:16px;color:#666;font-size:14px}}</style>
</head><body><main class="archive-page"><h1>{title}</h1>
<p class="archive-meta">旺哥 · AI应用实战派PRO · {record['date']}<br><a href="{source}">微信公众号原文（含在线互动与视频）</a></p>
{body}
<p class="archive-note">此页保留已下载原 HTML 的正文和样式，图片保存于同目录 images 文件夹；移除了微信页面控件。视频与小程序请查看公众号原文。正文与配图沿用仓库的 CC BY-NC-ND 4.0 许可。</p>
</main></body></html>'''
        article_html = folder / 'article.html'
        article_html.write_text(output, encoding='utf-8')
        record['html_path'] = article_html.relative_to(root).as_posix()
        record['html_download_url'] = f"{DOWNLOAD_BASE}/{record['id']}.zip"
        record['local_image_count'] = len(image_files)
        markdown_file = root / record['article_path']
        markdown = markdown_file.read_text(encoding='utf-8-sig')
        def replace_image(match):
            url = normalized_url(match.group(2))
            if url not in replacements:
                return match.group(0)
            relative = '../../' + replacements[url]
            return f'![{match.group(1)}]({relative})'
        markdown = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_image, markdown)
        # Image-only paragraphs do not need Markdown's trailing-space line break.
        markdown = '\n'.join(line.rstrip() if '../../html/' in line else line for line in markdown.splitlines())+'\n'
        link = f"> [下载 HTML 图文包（含图片）]({record['html_download_url']}) · [HTML 文件](../../{record['html_path']})"
        markdown = re.sub(r'(?m)^> \[下载 HTML 图文包（含图片）\].*\n?', '', markdown)
        markdown = re.sub(r'(?m)(^> \[查看微信公众号原文\]\([^\n]+\))', lambda match: match.group(1)+'\n'+link, markdown, count=1)
        markdown_file.write_text(markdown, encoding='utf-8')
        package_readme = folder / '阅读说明.txt'
        package_readme.write_text(f"{record['title']}\n\n解压整个 ZIP，再双击 article.html，用浏览器阅读。请保留 images 文件夹。\n图片可离线查看；视频和小程序在公众号原文中查看。\n原文：{record['source_url']}\n\nHTML 正文与样式来自已有原网页，非 Markdown 重新转换；只增加阅读页头、移除页面控件和本地化图片。\n正文与配图许可：CC BY-NC-ND 4.0。\n", encoding='utf-8')
        files = [(article_html, 'article.html'), (package_readme, package_readme.name)]
        files.extend((folder / p, p) for p in image_files)
        write_zip(packages / f"{record['id']}.zip", files)
        summaries.append({'id': record['id'], 'images': len(image_files), 'html_path': record['html_path']})

    (root / 'metadata/articles.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    fields = list(dict.fromkeys(k for record in records for k in record))
    with (root / 'metadata/articles.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    readme = (root / 'README.md').read_text(encoding='utf-8-sig')
    readme = readme.replace('| 日期 | 文章 | 主题 | 原文 |', '| 日期 | 文章 | 主题 | HTML下载 | 原文 |').replace('|---|---|---|---|', '|---|---|---|---|---|')
    by_path = {r['article_path']: r for r in records}
    lines = []
    for line in readme.splitlines():
        if line.startswith('| ') and '](articles/' in line:
            path = re.search(r'\]\((articles/[^)]+)\)', line).group(1)
            record = by_path[path]
            if record.get('html_download_url'):
                if '下载图文包' in line:
                    line = re.sub(r'\[下载图文包\]\([^)]*\)', f"[下载图文包]({record['html_download_url']})", line)
                else:
                    line = line.replace('| [公众号原文]', f"| [下载图文包]({record['html_download_url']}) | [公众号原文]")
            else:
                line = line.replace('| [公众号原文]', '| 待补齐 | [公众号原文]')
        lines.append(line)
    readme = '\n'.join(lines)+'\n'
    if '## HTML 图文下载' not in readme:
        instructions = f'''## HTML 图文下载

- **单篇下载**：点击下表的“下载图文包”，解压后双击 `article.html`；保留配套的 `images` 文件夹，正文图片可离线查看。
- **全部下载**：[全部文章 HTML 图文合集（仓库 ZIP）](https://github.com/{REPOSITORY}/archive/refs/heads/main.zip)。解压后进入 `wangge-articles-main` 文件夹，打开 `index.html`，按标题选择文章。
- GitHub 里的 Markdown 正文也已改为仓库本地图片。HTML 取自已下载原网页的正文和样式，保留原有图文排版；只增加阅读页头、移除微信页面控件和本地化图片。视频、小程序和互动在公众号原文中查看。
- GitHub 文件页显示 HTML 源码，带图阅读请下载图文包并在浏览器打开。

'''
        readme = readme.replace('## 全部文章\n', instructions+'## 全部文章\n')
    (root / 'README.md').write_text(readme, encoding='utf-8')
    for topic_file in (root / 'indexes/topics').glob('*.md'):
        text = topic_file.read_text(encoding='utf-8-sig')
        for record in records:
            marker = f"](../../{record['article_path']})"
            if marker in text and record.get('html_download_url'):
                rows = []
                for line in text.splitlines():
                    if marker in line and '[HTML图文包]' not in line:
                        line += f" · [HTML图文包]({record['html_download_url']})"
                    rows.append(line)
                text = '\n'.join(rows)+'\n'
        topic_file.write_text(text, encoding='utf-8')
    available = [r for r in records if r.get('html_path')]
    index_path = work / 'index.html'
    links = '\n'.join(f"<li>{r['date']} · <a href=\"{r['html_path']}\">{html.escape(r['title'])}</a></li>" for r in reversed(available))
    index_path.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>旺哥文章 HTML 图文合集</title><style>body{max-width:900px;margin:32px auto;padding:0 20px;font:17px/1.8 sans-serif}li{margin:12px 0}</style><h1>旺哥文章 HTML 图文合集</h1><p>点击标题带图阅读；请保留 html 文件夹。视频与小程序需查看公众号原文。</p><ol>'+links+'</ol></html>', encoding='utf-8')
    shutil.copyfile(index_path, root / 'index.html')
    complete_files = [(index_path, 'index.html'), (root / 'LICENSE.md', 'LICENSE.md')]
    for record in available:
        folder = (root / record['html_path']).parent
        complete_files.extend((p, p.relative_to(root).as_posix()) for p in sorted(folder.rglob('*')) if p.is_file())
    write_zip(packages / 'wangge-articles-html.zip', complete_files)
    result = {'articles': len(selected), 'image_slots': sum(r['images'] for r in summaries), 'packages_dir': str(packages), 'articles_detail': summaries}
    (work / 'build-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'articles_detail'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
