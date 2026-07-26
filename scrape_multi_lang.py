import os
import re
import sys
import time
import urllib.parse
import concurrent.futures
from queue import Queue
import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

BASE_URL = "https://old.namspi.uz"
DOMAIN = "old.namspi.uz"
ROOT_DIR = os.path.abspath(".")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "uz,ru,en;q=0.9",
}

ASSET_EXTENSIONS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z", ".mp3", ".mp4"
}

IGNORE_HOSTS = {
    "mc.yandex.ru", "pagead2.googlesyndication.com", "cnt0.www.uz", "s01.flagcounter.com", "info.flagcounter.com"
}

def sanitize_filename(filename):
    filename = urllib.parse.unquote(filename)
    sanitized = re.sub(r'[<>:"|?*]', '_', filename)
    if len(sanitized) > 100:
        h = str(abs(hash(sanitized)))[:8]
        sanitized = sanitized[:90] + "_" + h
    return sanitized

def url_to_asset_rel_path(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc and parsed.netloc not in (DOMAIN, "www.namspi.uz"):
        if "fonts.googleapis.com" in parsed.netloc:
            q_hash = str(abs(hash(parsed.query)))
            return os.path.join("fonts", f"google_fonts_{q_hash}.css")
        elif "fonts.gstatic.com" in parsed.netloc:
            fname = sanitize_filename(os.path.basename(parsed.path))
            return os.path.join("fonts", fname)
        else:
            fname = sanitize_filename(os.path.basename(parsed.path)) or "ext_file"
            q_hash = str(abs(hash(url)))
            return os.path.join("external", f"{q_hash}_{fname}")

    path = parsed.path.strip('/')
    parts = [sanitize_filename(p) for p in path.split('/') if p]
    if not parts:
        return "assets_misc"
    return os.path.join(*parts)

def url_to_html_rel_path(url, lang_code):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip('/')
    query = parsed.query

    if not path:
        if query:
            q_safe = re.sub(r'[^a-zA-Z0-9_-]', '_', query)
            filename = f"index_{q_safe}.html"
        else:
            filename = "index.html"
        return os.path.join(lang_code, filename)

    parts = [sanitize_filename(p) for p in path.split('/') if p]
    if not parts:
        return os.path.join(lang_code, "index.html")

    ext = os.path.splitext(parts[-1])[1].lower()
    if ext:
        filename = parts[-1]
        sub = parts[:-1]
    else:
        last = parts[-1]
        if len(last) > 80:
            h = str(abs(hash(last)))[:8]
            last = last[:70] + "_" + h
            
        if query:
            q_safe = re.sub(r'[^a-zA-Z0-9_-]', '_', query)
            filename = f"{last}_{q_safe}.html"
        else:
            filename = f"{last}.html"
        sub = parts[:-1]

    if sub:
        return os.path.join(lang_code, *sub, filename)
    else:
        return os.path.join(lang_code, filename)

class MultiLangCrawler:
    def __init__(self, base_url, root_dir, max_workers=14):
        self.base_url = base_url
        self.root_dir = root_dir
        self.max_workers = max_workers
        self.downloaded_assets = set()

    def is_internal_url(self, url):
        parsed = urllib.parse.urlparse(url)
        return not parsed.netloc or parsed.netloc in (DOMAIN, "www.namspi.uz")

    def is_asset_url(self, url):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.lower()
        ext = os.path.splitext(path)[1]
        if ext in ASSET_EXTENSIONS:
            return True
        if "fonts.googleapis.com" in parsed.netloc or "fonts.gstatic.com" in parsed.netloc:
            return True
        if any(p in path for p in ["/uploads/", "/files/", "/front/", "/admin/", "/photos/", "/galleries/"]):
            if ext or not path.endswith('/'):
                return True
        return False

    def normalize_url(self, url, current_url):
        if not url or url.startswith('#') or url.startswith('javascript:') or url.startswith('mailto:') or url.startswith('tel:'):
            return None
        joined = urllib.parse.urljoin(current_url, url)
        parsed = urllib.parse.urlparse(joined)
        cleaned = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ''))
        return cleaned

    def get_relative_link(self, current_file_path, target_file_path):
        current_dir = os.path.dirname(current_file_path)
        rel = os.path.relpath(target_file_path, current_dir)
        return rel.replace('\\', '/')

    def download_asset_file(self, session, url, asset_rel_path):
        if url in self.downloaded_assets:
            return
        self.downloaded_assets.add(url)

        full_path = os.path.join(self.root_dir, asset_rel_path)
        if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
            return

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        try:
            r = session.get(url, timeout=25, stream=True)
            if r.status_code == 200:
                content_type = r.headers.get('Content-Type', '')
                if 'text/css' in content_type or asset_rel_path.endswith('.css'):
                    css_text = r.text
                    css_text = self.process_css_content(session, css_text, url, asset_rel_path)
                    with open(full_path, 'w', encoding='utf-8', errors='ignore') as f:
                        f.write(css_text)
                else:
                    with open(full_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=16384):
                            f.write(chunk)
                sys.stdout.write(f"[ASSET] Saved: {asset_rel_path}\n")
                sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(f"[ERROR ASSET] {url}: {e}\n")
            sys.stdout.flush()

    def process_css_content(self, session, css_content, css_url, css_rel_path):
        if not css_content:
            return ""

        def replace_url(match):
            raw_url = match.group(1).strip('\'"')
            if raw_url.startswith('data:'):
                return match.group(0)
            
            full_url = urllib.parse.urljoin(css_url, raw_url)
            parsed = urllib.parse.urlparse(full_url)
            if parsed.netloc in IGNORE_HOSTS:
                return match.group(0)
            
            target_asset_rel = url_to_asset_rel_path(full_url)
            self.download_asset_file(session, full_url, target_asset_rel)
            rel_link = self.get_relative_link(css_rel_path, target_asset_rel)
            return f"url('{rel_link}')"

        return re.sub(r'url\((.*?)\)', replace_url, css_content, flags=re.IGNORECASE)

    def crawl_language(self, lang_code):
        print(f"\n=============================================")
        print(f"   STARTING SCRAPE FOR LANGUAGE: [{lang_code.upper()}]")
        print(f"=============================================\n")

        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            session.get(f"{self.base_url}/lang/{lang_code}", timeout=15)
        except Exception as e:
            print(f"Error setting session lang {lang_code}: {e}")

        visited_urls = set()
        url_queue = Queue()

        start_url = f"{self.base_url}/"
        visited_urls.add(start_url)
        url_queue.put(start_url)

        crawled_count = 0

        def process_html_page(url):
            nonlocal crawled_count
            current_html_rel = url_to_html_rel_path(url, lang_code)
            full_html_path = os.path.join(self.root_dir, current_html_rel)

            try:
                r = session.get(url, timeout=25)
                if r.status_code != 200:
                    return

                soup = BeautifulSoup(r.text, 'html.parser')

                # Remove tracking scripts
                for s in soup.find_all('script'):
                    src = s.get('src', '')
                    if any(h in src for h in IGNORE_HOSTS):
                        s.decompose()

                # Process inline <style> blocks
                for st in soup.find_all('style'):
                    if st.string:
                        st.string = self.process_css_content(session, st.string, url, current_html_rel)

                # Process <a> links
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    
                    # Intercept language switcher buttons
                    if '/lang/uz' in href:
                        target_html = url_to_html_rel_path(self.base_url, 'uz')
                        a['href'] = self.get_relative_link(current_html_rel, target_html)
                        continue
                    elif '/lang/ru' in href:
                        target_html = url_to_html_rel_path(self.base_url, 'ru')
                        a['href'] = self.get_relative_link(current_html_rel, target_html)
                        continue
                    elif '/lang/us' in href or '/lang/en' in href:
                        target_html = url_to_html_rel_path(self.base_url, 'us')
                        a['href'] = self.get_relative_link(current_html_rel, target_html)
                        continue

                    full_link = self.normalize_url(href, url)
                    if not full_link:
                        continue

                    parsed_link = urllib.parse.urlparse(full_link)
                    if parsed_link.netloc in IGNORE_HOSTS:
                        continue

                    if self.is_internal_url(full_link):
                        if self.is_asset_url(full_link):
                            asset_rel = url_to_asset_rel_path(full_link)
                            self.download_asset_file(session, full_link, asset_rel)
                            a['href'] = self.get_relative_link(current_html_rel, asset_rel)
                        else:
                            target_html_rel = url_to_html_rel_path(full_link, lang_code)
                            a['href'] = self.get_relative_link(current_html_rel, target_html_rel)
                            if full_link not in visited_urls:
                                visited_urls.add(full_link)
                                url_queue.put(full_link)

                # Process images, scripts, stylesheets, media
                tag_attrs = [
                    ('img', 'src'), ('img', 'srcset'), ('script', 'src'),
                    ('link', 'href'), ('source', 'src'), ('source', 'srcset'),
                    ('iframe', 'src'), ('video', 'src'), ('audio', 'src')
                ]

                for tag_name, attr in tag_attrs:
                    for elem in soup.find_all(tag_name):
                        val = elem.get(attr)
                        if not val:
                            continue

                        if 'srcset' in attr:
                            new_srcset_parts = []
                            for part in val.split(','):
                                part_str = part.strip()
                                if not part_str:
                                    continue
                                bits = part_str.split()
                                part_url = bits[0]
                                full_part_url = self.normalize_url(part_url, url)
                                if full_part_url:
                                    part_asset_rel = url_to_asset_rel_path(full_part_url)
                                    self.download_asset_file(session, full_part_url, part_asset_rel)
                                    rel_link = self.get_relative_link(current_html_rel, part_asset_rel)
                                    rest = " ".join(bits[1:])
                                    new_srcset_parts.append(f"{rel_link} {rest}".strip())
                            elem[attr] = ", ".join(new_srcset_parts)
                        else:
                            full_asset_url = self.normalize_url(val, url)
                            if full_asset_url:
                                parsed_asset = urllib.parse.urlparse(full_asset_url)
                                if parsed_asset.netloc not in IGNORE_HOSTS:
                                    asset_rel = url_to_asset_rel_path(full_asset_url)
                                    self.download_asset_file(session, full_asset_url, asset_rel)
                                    elem[attr] = self.get_relative_link(current_html_rel, asset_rel)

                # Inline style url(...) rewriting
                for elem in soup.find_all(style=True):
                    style_val = elem['style']
                    def replace_inline_url(match):
                        raw_u = match.group(1).strip('\'"')
                        full_u = self.normalize_url(raw_u, url)
                        if full_u:
                            t_asset_rel = url_to_asset_rel_path(full_u)
                            self.download_asset_file(session, full_u, t_asset_rel)
                            r_link = self.get_relative_link(current_html_rel, t_asset_rel)
                            return f"url('{r_link}')"
                        return match.group(0)
                    elem['style'] = re.sub(r'url\((.*?)\)', replace_inline_url, style_val, flags=re.IGNORECASE)

                # Save HTML file locally
                os.makedirs(os.path.dirname(full_html_path), exist_ok=True)
                with open(full_html_path, 'w', encoding='utf-8') as f:
                    f.write(soup.prettify())

                crawled_count += 1
                sys.stdout.write(f"[{lang_code.upper()} #{crawled_count}] Saved: {current_html_rel}\n")
                sys.stdout.flush()

            except Exception as e:
                sys.stdout.write(f"[{lang_code.upper()} ERROR] {url}: {e}\n")
                sys.stdout.flush()

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            active_futures = set()

            while True:
                while len(active_futures) < self.max_workers and not url_queue.empty():
                    target_url = url_queue.get()
                    future = executor.submit(process_html_page, target_url)
                    active_futures.add(future)

                if not active_futures:
                    break

                done, active_futures = concurrent.futures.wait(
                    active_futures, return_when=concurrent.futures.FIRST_COMPLETED
                )

                for f in done:
                    try:
                        f.result()
                    except Exception as exc:
                        pass

        print(f"[{lang_code.upper()} COMPLETED] Total pages: {crawled_count}")

def create_root_index():
    html_content = """<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Namangan Davlat Pedagogika Instituti (Offlayn Arxiv)</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #f8fafc;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            max-width: 800px;
            width: 100%;
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(16px);
            border-radius: 24px;
            padding: 40px 30px;
            text-align: center;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
        }
        h1 {
            font-size: 2.2rem;
            font-weight: 700;
            margin-bottom: 12px;
            background: linear-gradient(to right, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        p.subtitle {
            color: #94a3b8;
            font-size: 1.1rem;
            margin-bottom: 40px;
        }
        .lang-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .lang-card {
            background: rgba(51, 65, 85, 0.6);
            border: 2px solid rgba(255, 255, 255, 0.08);
            border-radius: 18px;
            padding: 25px 20px;
            text-decoration: none;
            color: #ffffff;
            transition: all 0.3s ease;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        .lang-card:hover {
            transform: translateY(-5px);
            background: rgba(56, 189, 248, 0.15);
            border-color: #38bdf8;
            box-shadow: 0 10px 25px -5px rgba(56, 189, 248, 0.3);
        }
        .flag {
            font-size: 3rem;
            margin-bottom: 12px;
        }
        .lang-name {
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 4px;
        }
        .lang-desc {
            font-size: 0.85rem;
            color: #cbd5e1;
        }
        footer {
            margin-top: 30px;
            font-size: 0.85rem;
            color: #64748b;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Namangan Davlat Pedagogika Instituti</h1>
        <p class="subtitle">Offlayn Arxiv Sayti - Kerakli tilni tanlang:</p>
        
        <div class="lang-grid">
            <a href="uz/index.html" class="lang-card">
                <div class="flag">🇺🇿</div>
                <div class="lang-name">O'zbekcha</div>
                <div class="lang-desc">O'zbek tilidagi to'liq offlayn sayt</div>
            </a>
            
            <a href="ru/index.html" class="lang-card">
                <div class="flag">🇷🇺</div>
                <div class="lang-name">Русский</div>
                <div class="lang-desc">Полная офлайн версия на русском</div>
            </a>
            
            <a href="us/index.html" class="lang-card">
                <div class="flag">🇬🇧</div>
                <div class="lang-name">English</div>
                <div class="lang-desc">Full offline version in English</div>
            </a>
        </div>
        
        <footer>
            Intellektual Arxiv Tizimi &bull; Server talab qilinmaydi &bull; HTML fayllari brauzerda bevosita ishlaydi
        </footer>
    </div>
</body>
</html>"""
    with open(os.path.join(ROOT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_content)
    print("Created root index.html offline language selector!")

if __name__ == "__main__":
    crawler = MultiLangCrawler(BASE_URL, ROOT_DIR, max_workers=14)
    create_root_index()
    for lang in ['uz', 'ru', 'us']:
        crawler.crawl_language(lang)
