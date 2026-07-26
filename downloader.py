import os
import re
import sys
import time
import urllib.parse
import concurrent.futures
from queue import Queue, Empty
import requests
from bs4 import BeautifulSoup

# Ensure UTF-8 output encoding for Windows terminal output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

BASE_URL = "https://old.namspi.uz"
DOMAIN = "old.namspi.uz"
OUTPUT_DIR = os.path.abspath(".")

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
    return re.sub(r'[<>:"|?*]', '_', filename)

def url_to_local_rel_path(url):
    """
    Converts a full or absolute URL to a relative local file path.
    """
    parsed = urllib.parse.urlparse(url)
    
    # Handle external Google Fonts CSS/fonts or other resources
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
    query = parsed.query

    if not path:
        if query:
            q_safe = re.sub(r'[^a-zA-Z0-9_-]', '_', query)
            return f"index_{q_safe}.html"
        return "index.html"

    parts = [sanitize_filename(p) for p in path.split('/') if p]

    if not parts:
        return "index.html"

    ext = os.path.splitext(parts[-1])[1].lower()
    
    if ext:
        return os.path.join(*parts)
    else:
        if query:
            q_safe = re.sub(r'[^a-zA-Z0-9_-]', '_', query)
            last = f"{parts[-1]}_{q_safe}.html"
        else:
            last = f"{parts[-1]}.html"
        
        if len(parts) > 1:
            return os.path.join(*parts[:-1], last)
        else:
            return last

class SiteCrawler:
    def __init__(self, base_url, output_dir, max_workers=16):
        self.base_url = base_url
        self.output_dir = output_dir
        self.max_workers = max_workers
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        
        self.visited_urls = set()
        self.downloaded_assets = set()
        self.url_queue = Queue()
        
        self.crawled_count = 0
        self.asset_count = 0
        self.failed_urls = set()

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
        if any(p in path for p in ["/uploads/", "/files/", "/front/", "/admin/", "/photos/"]):
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

    def get_relative_link(self, current_rel_path, target_rel_path):
        current_dir = os.path.dirname(current_rel_path)
        if not current_dir:
            return target_rel_path.replace('\\', '/')
        rel = os.path.relpath(target_rel_path, current_dir)
        return rel.replace('\\', '/')

    def process_css_content(self, css_content, css_url, css_rel_path):
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
            
            target_rel_path = url_to_local_rel_path(full_url)
            self.download_asset_file(full_url, target_rel_path)
            rel_link = self.get_relative_link(css_rel_path, target_rel_path)
            return f"url('{rel_link}')"

        updated_css = re.sub(r'url\((.*?)\)', replace_url, css_content, flags=re.IGNORECASE)
        return updated_css

    def download_asset_file(self, url, rel_path):
        if url in self.downloaded_assets:
            return
        self.downloaded_assets.add(url)
        
        full_path = os.path.join(self.output_dir, rel_path)
        if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
            return

        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        try:
            r = self.session.get(url, timeout=25, stream=True)
            if r.status_code == 200:
                content_type = r.headers.get('Content-Type', '')
                if 'text/css' in content_type or rel_path.endswith('.css'):
                    css_text = r.text
                    css_text = self.process_css_content(css_text, url, rel_path)
                    with open(full_path, 'w', encoding='utf-8', errors='ignore') as f:
                        f.write(css_text)
                else:
                    with open(full_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=16384):
                            f.write(chunk)
                self.asset_count += 1
                sys.stdout.write(f"[ASSET {self.asset_count}] Saved: {rel_path}\n")
                sys.stdout.flush()
            else:
                self.failed_urls.add(url)
        except Exception as e:
            sys.stdout.write(f"[ERROR ASSET] Failed {url}: {e}\n")
            sys.stdout.flush()
            self.failed_urls.add(url)

    def process_html_page(self, url):
        current_rel_path = url_to_local_rel_path(url)
        full_path = os.path.join(self.output_dir, current_rel_path)
        
        try:
            r = self.session.get(url, timeout=25)
            if r.status_code != 200:
                sys.stdout.write(f"[HTTP {r.status_code}] {url}\n")
                sys.stdout.flush()
                self.failed_urls.add(url)
                return

            soup = BeautifulSoup(r.text, 'html.parser')
            
            # Neutralize tracking/analytics scripts
            for s in soup.find_all('script'):
                src = s.get('src', '')
                if any(h in src for h in IGNORE_HOSTS):
                    s.decompose()

            # Process <style> blocks
            for st in soup.find_all('style'):
                if st.string:
                    st.string = self.process_css_content(st.string, url, current_rel_path)

            # Process <a> links
            for a in soup.find_all('a', href=True):
                href = a['href']
                full_link = self.normalize_url(href, url)
                if not full_link:
                    continue
                
                parsed_link = urllib.parse.urlparse(full_link)
                if parsed_link.netloc in IGNORE_HOSTS:
                    continue

                if self.is_internal_url(full_link):
                    target_rel = url_to_local_rel_path(full_link)
                    a['href'] = self.get_relative_link(current_rel_path, target_rel)
                    
                    if self.is_asset_url(full_link):
                        self.download_asset_file(full_link, target_rel)
                    else:
                        if full_link not in self.visited_urls:
                            self.visited_urls.add(full_link)
                            self.url_queue.put(full_link)

            # Process images, scripts, links, media, sources
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
                                part_rel = url_to_local_rel_path(full_part_url)
                                self.download_asset_file(full_part_url, part_rel)
                                rel_link = self.get_relative_link(current_rel_path, part_rel)
                                rest = " ".join(bits[1:])
                                new_srcset_parts.append(f"{rel_link} {rest}".strip())
                        elem[attr] = ", ".join(new_srcset_parts)
                    else:
                        full_asset_url = self.normalize_url(val, url)
                        if full_asset_url:
                            parsed_asset = urllib.parse.urlparse(full_asset_url)
                            if parsed_asset.netloc not in IGNORE_HOSTS:
                                target_rel = url_to_local_rel_path(full_asset_url)
                                self.download_asset_file(full_asset_url, target_rel)
                                elem[attr] = self.get_relative_link(current_rel_path, target_rel)

            # Inline style url(...) rewriting
            for elem in soup.find_all(style=True):
                style_val = elem['style']
                def replace_inline_url(match):
                    raw_u = match.group(1).strip('\'"')
                    full_u = self.normalize_url(raw_u, url)
                    if full_u:
                        t_rel = url_to_local_rel_path(full_u)
                        self.download_asset_file(full_u, t_rel)
                        r_link = self.get_relative_link(current_rel_path, t_rel)
                        return f"url('{r_link}')"
                    return match.group(0)
                elem['style'] = re.sub(r'url\((.*?)\)', replace_inline_url, style_val, flags=re.IGNORECASE)

            # Save modified HTML page locally
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(soup.prettify())
            
            self.crawled_count += 1
            sys.stdout.write(f"[PAGE {self.crawled_count}] Saved HTML: {current_rel_path}\n")
            sys.stdout.flush()

        except Exception as e:
            sys.stdout.write(f"[ERROR PAGE] {url}: {e}\n")
            sys.stdout.flush()
            self.failed_urls.add(url)

    def start_crawl(self):
        print("=== STARTING FULL OFFLINE ARCHIVE SCRAPE ===")
        start_time = time.time()
        
        initial_urls = [
            BASE_URL,
            f"{BASE_URL}/lang/uz",
            f"{BASE_URL}/lang/ru",
            f"{BASE_URL}/lang/us",
        ]
        
        for u in initial_urls:
            self.visited_urls.add(u)
            self.url_queue.put(u)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            active_futures = set()

            while True:
                while len(active_futures) < self.max_workers and not self.url_queue.empty():
                    target_url = self.url_queue.get()
                    future = executor.submit(self.process_html_page, target_url)
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
                        sys.stdout.write(f"[TASK EXCEPTION] {exc}\n")
                        sys.stdout.flush()

        elapsed = time.time() - start_time
        print("\n=============================================")
        print(f"SCRAPE FINISHED in {elapsed:.2f} seconds.")
        print(f"Total HTML pages crawled: {self.crawled_count}")
        print(f"Total static assets downloaded: {self.asset_count}")
        print(f"Failed items count: {len(self.failed_urls)}")
        print("=============================================\n")

if __name__ == "__main__":
    crawler = SiteCrawler(BASE_URL, OUTPUT_DIR, max_workers=16)
    crawler.start_crawl()
