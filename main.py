from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, OpenerDirector, Request, build_opener

from pdf_parser import parse_pdf, write_parsed_json

BASE_URL = "https://edu.stankin.ru"
LOGIN_URL = f"{BASE_URL}/login/index.php"
COURSE_URL = f"{BASE_URL}/course/view.php?id=11557"
DEFAULT_TIMEOUT = 20
AUTH_COOKIES_PATH = Path("auth_cookies.json")
SCHEDULE_LINKS_PATH = Path("schedule_links.json")
PDFS_DIR = Path("pdfs")
GROUPS_INDEX_PATH = PDFS_DIR / "groups.json"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.9",
}


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


@dataclass(slots=True)
class Config:
    login: str
    password: str

    @classmethod
    def from_env(cls) -> "Config":
        login = os.getenv("SCHEDULE_LOGIN")
        password = os.getenv("SCHEDULE_PASSWORD")
        if not login or not password:
            raise ValueError("Set SCHEDULE_LOGIN and SCHEDULE_PASSWORD in .env or env")
        return cls(login=login, password=password)


@dataclass(slots=True)
class HTTPResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(slots=True)
class LoginPageSnapshot:
    title: str | None
    form_action: str
    form_method: str
    hidden_inputs: dict[str, str]
    sesskey: str | None


@dataclass(slots=True)
class AuthState:
    final_url: str
    status: int
    is_logged_in: bool
    sesskey: str | None
    cookie_names: list[str]
    cookies: list[dict[str, str]]


@dataclass(slots=True)
class ScheduleLink:
    section: str
    context: str | None
    title: str
    url: str


@dataclass(slots=True)
class PDFLink:
    title: str
    url: str
    filename: str


class LoginPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_title = False
        self._active_form_id: str | None = None
        self._title_parts: list[str] = []
        self.form_action = LOGIN_URL
        self.form_method = "post"
        self.hidden_inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)

        if tag == "title":
            self._inside_title = True
            return

        if tag == "form":
            self._active_form_id = attr_map.get("id")
            if self._active_form_id == "login":
                self.form_action = attr_map.get("action") or LOGIN_URL
                self.form_method = (attr_map.get("method") or "post").lower()
            return

        if tag == "input" and self._active_form_id == "login":
            bound_form = attr_map.get("form")
            if bound_form and bound_form != "login":
                return
            name = attr_map.get("name")
            input_type = (attr_map.get("type") or "text").lower()
            if name and input_type == "hidden":
                self.hidden_inputs[name] = attr_map.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._inside_title = False
        elif tag == "form":
            self._active_form_id = None

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text and self._inside_title:
            self._title_parts.append(text)

    def build_snapshot(self, html: str) -> LoginPageSnapshot:
        sesskey_match = re.search(r'"sesskey":"([^"]+)"', html)
        title = " ".join(self._title_parts).strip() or None
        return LoginPageSnapshot(
            title=title,
            form_action=self.form_action,
            form_method=self.form_method,
            hidden_inputs=self.hidden_inputs,
            sesskey=sesskey_match.group(1) if sesskey_match else None,
        )


class FolderPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._active_href: str | None = None
        self._active_text: list[str] = []
        self._seen_urls: set[str] = set()
        self.pdf_links: list[PDFLink] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        self._active_href = href
        self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_href is None:
            return
        self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._active_href is None:
            return

        href = unescape(self._active_href).strip()
        title = re.sub(r"\s+", " ", unescape("".join(self._active_text))).strip()
        filename = self._extract_filename(href, title)

        if filename and href not in self._seen_urls:
            self._seen_urls.add(href)
            self.pdf_links.append(
                PDFLink(
                    title=title or filename,
                    url=urljoin(BASE_URL, href),
                    filename=filename,
                )
            )

        self._active_href = None
        self._active_text = []

    @staticmethod
    def _extract_filename(href: str, title: str) -> str | None:
        title_name = title.strip()
        if title_name.lower().endswith(".pdf"):
            return Path(title_name).name

        path_name = Path(unquote(urlsplit(href).path)).name
        if path_name.lower().endswith(".pdf"):
            return path_name

        return None


class StankinClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.cookie_jar = CookieJar()
        self.opener: OpenerDirector = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.sesskey: str | None = None

    def fetch_login_page(self) -> HTTPResult:
        return self._request(LOGIN_URL, "GET")

    def parse_login_page(self, html: str) -> LoginPageSnapshot:
        parser = LoginPageParser()
        parser.feed(html)
        return parser.build_snapshot(html)

    def authenticate(self, snapshot: LoginPageSnapshot | None = None) -> AuthState:
        login_page = snapshot or self.parse_login_page(self.fetch_login_page().text)
        payload = {
            "username": self.config.login,
            "password": self.config.password,
            "anchor": login_page.hidden_inputs.get("anchor", ""),
            "logintoken": login_page.hidden_inputs.get("logintoken", ""),
        }

        result = self._request(
            url=login_page.form_action or LOGIN_URL,
            method=login_page.form_method.upper(),
            data=urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
                "Referer": LOGIN_URL,
            },
        )

        sesskey_match = re.search(r'"sesskey":"([^"]+)"', result.text)
        self.sesskey = sesskey_match.group(1) if sesskey_match else None
        cookie_names = [cookie.name for cookie in self.cookie_jar]
        is_logged_in = (
            result.url.rstrip("/") != LOGIN_URL.rstrip("/")
            or "loginerrors" not in result.text
            and 'id="login"' not in result.text
        )

        return AuthState(
            final_url=result.url,
            status=result.status,
            is_logged_in=is_logged_in,
            sesskey=self.sesskey,
            cookie_names=cookie_names,
            cookies=self.export_cookies(),
        )

    def fetch_course_page(self) -> HTTPResult:
        return self._request(COURSE_URL, "GET")

    def fetch_page(self, url: str, *, referer: str | None = None) -> HTTPResult:
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        return self._request(url, "GET", headers=headers or None)

    def export_cookies(self) -> list[dict[str, str]]:
        cookies: list[dict[str, str]] = []
        for cookie in self.cookie_jar:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                }
            )
        return cookies

    def parse_schedule_links(self, html: str) -> list[ScheduleLink]:
        section_pattern = re.compile(r'data-sectionname="([^"]+)"')
        section_matches = list(section_pattern.finditer(html))
        marker_text = "Расписание занятий"
        activity_pattern = re.compile(
            r'<li\b[^>]*class="activity activity-wrapper[^"]*"[^>]*>(.*?)</li>',
            re.S,
        )
        link_pattern = re.compile(
            r'<a href="([^"]+)" class="[^"]*\baalink\b[^"]*"[^>]*>\s*'
            r'<span class="instancename">\s*([^<]+?)\s*<span class="accesshide',
            re.S,
        )

        links: list[ScheduleLink] = []
        for index, match in enumerate(section_matches):
            section_name = unescape(match.group(1)).strip()
            section_start = match.start()
            section_end = (
                section_matches[index + 1].start()
                if index + 1 < len(section_matches)
                else len(html)
            )
            section_html = html[section_start:section_end]
            if marker_text not in section_html:
                continue

            marker_found = False
            current_context: str | None = None

            for activity_html in activity_pattern.findall(section_html):
                activity_name_match = re.search(
                    r'data-activityname="([^"]+)"',
                    activity_html,
                )
                if not activity_name_match:
                    continue

                activity_name = re.sub(
                    r"\s+",
                    " ",
                    unescape(activity_name_match.group(1)),
                ).strip()
                link_match = link_pattern.search(activity_html)

                if activity_name == marker_text:
                    marker_found = True
                    if link_match:
                        links.append(
                            ScheduleLink(
                                section=section_name,
                                context=current_context,
                                title=activity_name,
                                url=unescape(link_match.group(1)).strip(),
                            )
                        )
                    continue

                if not marker_found:
                    continue

                if not link_match:
                    current_context = activity_name
                    continue

                link_title = re.sub(
                    r"\s+",
                    " ",
                    unescape(link_match.group(2)),
                ).strip()
                links.append(
                    ScheduleLink(
                        section=section_name,
                        context=current_context,
                        title=link_title,
                        url=unescape(link_match.group(1)).strip(),
                    )
                )

        return links

    def parse_folder_pdf_links(self, html: str) -> list[PDFLink]:
        parser = FolderPageParser()
        parser.feed(html)
        return parser.pdf_links

    def _request(
        self,
        url: str,
        method: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> HTTPResult:
        request_headers = DEFAULT_HEADERS.copy()
        if headers:
            request_headers.update(headers)

        request = Request(url=url, data=data, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=DEFAULT_TIMEOUT) as response:
                return HTTPResult(
                    url=response.geturl(),
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {url}: {details[:500]}") from exc
        except URLError as exc:
            raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc


def schedule_links_payload(items: list[ScheduleLink]) -> list[dict[str, str | None]]:
    return [
        {
            "section": item.section,
            "context": item.context,
            "title": item.title,
            "url": item.url,
        }
        for item in items
    ]


def group_json_path(group_name: str) -> Path:
    return PDFS_DIR / f"{group_name}.json"


def sync_pdfs(client: StankinClient, schedule_links: list[ScheduleLink]) -> dict[str, object]:
    PDFS_DIR.mkdir(exist_ok=True)

    groups_index: dict[str, dict[str, str]] = {}
    folder_reports: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    duplicates: list[str] = []
    downloaded_count = 0

    for folder in schedule_links:
        try:
            folder_page = client.fetch_page(folder.url, referer=COURSE_URL)
            pdf_links = client.parse_folder_pdf_links(folder_page.text)
        except Exception as exc:
            errors.append(
                {
                    "scope": "folder",
                    "folder_url": folder.url,
                    "message": str(exc),
                }
            )
            continue

        folder_reports.append(
            {
                "section": folder.section,
                "context": folder.context,
                "title": folder.title,
                "url": folder.url,
                "pdf_count": len(pdf_links),
            }
        )

        for pdf_link in pdf_links:
            group_name = Path(pdf_link.filename).stem
            pdf_path = PDFS_DIR / f"{group_name}.pdf"
            json_path = group_json_path(group_name)

            if group_name in groups_index:
                duplicates.append(group_name)
                continue

            try:
                pdf_result = client.fetch_page(pdf_link.url, referer=folder.url)
                pdf_path.write_bytes(pdf_result.body)

                parsed = parse_pdf(pdf_path)
                write_parsed_json(pdf_path, parsed, output_path=json_path)
                groups_index[group_name] = {
                    "group_name": group_name,
                    "group_link": json_path.name,
                }
                downloaded_count += 1
            except Exception as exc:
                errors.append(
                    {
                        "scope": "pdf",
                        "group_name": group_name,
                        "pdf_url": pdf_link.url,
                        "message": str(exc),
                    }
                )

    groups_payload = sorted(groups_index.values(), key=lambda item: item["group_name"])
    GROUPS_INDEX_PATH.write_text(
        json.dumps(groups_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "pdfs_dir": str(PDFS_DIR),
        "groups_index_path": str(GROUPS_INDEX_PATH),
        "folders_processed": len(folder_reports),
        "downloaded_groups": downloaded_count,
        "groups_index_count": len(groups_payload),
        "duplicates": sorted(set(duplicates)),
        "errors": errors,
        "folder_reports": folder_reports,
    }


def main() -> None:
    load_dotenv()
    config = Config.from_env()
    client = StankinClient(config)

    login_page = client.fetch_login_page()
    login_snapshot = client.parse_login_page(login_page.text)
    auth_state = client.authenticate(login_snapshot)

    AUTH_COOKIES_PATH.write_text(
        json.dumps(auth_state.cookies, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    course_page = client.fetch_course_page()
    schedule_links = client.parse_schedule_links(course_page.text)
    links_payload = schedule_links_payload(schedule_links)
    SCHEDULE_LINKS_PATH.write_text(
        json.dumps(links_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    sync_report = sync_pdfs(client, schedule_links)

    print(
        json.dumps(
            {
                "login_page_title": login_snapshot.title,
                "login_status": auth_state.status,
                "is_logged_in": auth_state.is_logged_in,
                "final_url": auth_state.final_url,
                "sesskey": auth_state.sesskey,
                "cookie_names": auth_state.cookie_names,
                "cookies_saved_to": str(AUTH_COOKIES_PATH),
                "course_url": course_page.url,
                "course_status": course_page.status,
                "schedule_links_saved_to": str(SCHEDULE_LINKS_PATH),
                "schedule_links_count": len(links_payload),
                "sync_report": sync_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
