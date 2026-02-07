import json
import re
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .utils import clean_text, split_name_title_suffix


USER_AGENT = "StaffSearchBot/1.0 (+contact: canvaslens@example.com)"
SKIP_HOSTS = (
    "livrepository.liverpool.ac.uk",
)
SKIP_PATH_PREFIXES = (
    "/media/",
    "/news/",
    "/events/",
    "/courses/",
    "/search/",
    "/rb/",
    "/assets/",
    "/student",
    "/study/",
    "/cgi/stats/report/",
)
SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".zip", ".rar", ".7z",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp4", ".mp3", ".mov", ".avi",
    ".css", ".js", ".ico",
)


def normalize_url(url):
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = parsed._replace(scheme="https")
    elif parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    normalized = parsed._replace(netloc=netloc, path=path, query="", fragment="")
    return urlunparse(normalized)


def is_allowed(url, allow_domain):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return host == allow_domain or host.endswith("." + allow_domain)


def is_staff_profile_path(path, keep_regex):
    return re.match(keep_regex, path or "") is not None


def should_skip_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in SKIP_HOSTS:
        return True
    path = (parsed.path or "/").lower()
    for prefix in SKIP_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    for ext in SKIP_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def extract_links(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    links = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if href and not href.startswith("mailto:") and not href.startswith("tel:"):
            links.add(urljoin(base_url, href))
    return links


def extract_text_content(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"]))
    body = soup.get_text(" ", strip=True)

    return clean_text(" ".join([title, headings, body]))


def extract_labeled_fields(soup, label):
    values = []
    for dt in soup.find_all("dt"):
        if clean_text(dt.get_text()).lower() == label.lower():
            dd = dt.find_next_sibling("dd")
            if dd:
                values.append(clean_text(dd.get_text(" ", strip=True)))
    for p in soup.find_all("p"):
        text = clean_text(p.get_text(" ", strip=True))
        if text.lower().startswith(label.lower() + ":"):
            values.append(clean_text(text.split(":", 1)[1]))
    return values


def extract_staff_fields(html, base_url=""):
    soup = BeautifulSoup(html, "lxml")
    name_text = ""
    h1 = soup.find("h1")
    if h1:
        name_text = clean_text(h1.get_text(" ", strip=True))

    title, name, suffix = split_name_title_suffix(name_text)

    faculty = ""
    institute = ""
    department = ""

    meta_dept = soup.find("meta", attrs={"name": "uol.deptschool"})
    if meta_dept and meta_dept.get("content"):
        department = clean_text(meta_dept.get("content"))

    letters = soup.select_one(".rb-people__letters")
    if letters:
        letters_text = clean_text(letters.get_text(" ", strip=True))
        if letters_text:
            suffix = letters_text

    def extract_jsonld_suffix():
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "Person" and item.get("honorificSuffix"):
                    return clean_text(item.get("honorificSuffix"))
        return ""

    jsonld_suffix = extract_jsonld_suffix()
    if jsonld_suffix and (not suffix or len(jsonld_suffix) > len(suffix)):
        suffix = jsonld_suffix

    faculty_values = extract_labeled_fields(soup, "Faculty")
    if faculty_values:
        faculty = faculty_values[0]

    institute_values = extract_labeled_fields(soup, "Institute")
    if institute_values:
        institute = institute_values[0]

    department_values = extract_labeled_fields(soup, "Department")
    if department_values:
        department = department_values[0]

    if not (faculty and institute and department):
        header = soup.select_one(".rb-people__header__card")
        if header:
            for block in header.select(".rb-card__text"):
                strong = block.find("strong")
                if strong and clean_text(strong.get_text()).lower() == "part of":
                    links = block.find_all("a")
                    if not institute and len(links) > 0:
                        institute = clean_text(links[0].get_text(" ", strip=True))
                    if not faculty and len(links) > 1:
                        faculty = clean_text(links[1].get_text(" ", strip=True))
                else:
                    link = block.find("a")
                    if link and not department:
                        department = clean_text(link.get_text(" ", strip=True))
                    if not department:
                        block_text = block.get_text("\n", strip=True)
                        if strong:
                            strong_text = clean_text(strong.get_text(" ", strip=True))
                            block_text = block_text.replace(strong_text, "", 1).strip()
                        if block_text:
                            first_line = clean_text(block_text.split("\n")[0])
                            if first_line:
                                department = first_line

            if not institute:
                inst_link = header.find("a", string=re.compile(r"\bInstitute\b", re.I))
                if inst_link:
                    institute = clean_text(inst_link.get_text(" ", strip=True))

            if not faculty:
                fac_link = header.find("a", string=re.compile(r"\bFaculty\b", re.I))
                if fac_link:
                    faculty = clean_text(fac_link.get_text(" ", strip=True))

    if suffix:
        suffix_tokens = [t.strip() for t in suffix.split(",") if t.strip()]
        if suffix_tokens:
            suffix_pattern = re.compile(
                r"(,?\s+)" + r"\s*,\s*".join(re.escape(t) for t in suffix_tokens) + r"\s*$",
                re.I,
            )
            name = clean_text(suffix_pattern.sub("", name))

    if suffix:
        def slug_from_url(url):
            if not url:
                return ""
            parsed = urlparse(url)
            path = (parsed.path or "").strip("/")
            if not path:
                return ""
            last = path.split("/")[-1]
            return clean_text(last.replace("-", " "))

        canonical = ""
        canonical_tag = soup.find("link", rel="canonical")
        if canonical_tag and canonical_tag.get("href"):
            canonical = canonical_tag.get("href")

        slug_text = slug_from_url(canonical) or slug_from_url(base_url)
        suffix_text = clean_text(suffix.replace(",", " ")).lower()
        if slug_text and suffix_text and suffix_text in slug_text.lower():
            name = clean_text(f"{name} {suffix}")
            suffix = ""

    return {
        "name": name,
        "title": title,
        "suffix": suffix,
        "faculty": faculty,
        "institute": institute,
        "department": department,
    }


def fetch_url(url, etag=None, last_modified=None, timeout=20):
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    response = requests.get(url, headers=headers, timeout=timeout)
    return response
