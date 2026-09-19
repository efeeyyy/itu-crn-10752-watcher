import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, Optional, Tuple

BASE = "https://obs.itu.edu.tr/public/DersProgram"
PROGRAM_LEVEL = "LS"
BRANCH_CODE = "SNT"
CRN = "10752"
EXPECTED_COURSE_CODE = "SNT 114E"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()

STATE_TITLE = f"[state] ITU CRN watcher {CRN}"
USER_AGENT = "itu-crn-10752-watcher/1.1"


def request_bytes(url: str, *, params=None, data=None, headers=None, method=None) -> bytes:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
    merged = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
    }
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=body, headers=merged, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def request_json(url: str, *, params=None, payload=None, headers=None, method=None):
    merged = {"Accept": "application/json"}
    if headers:
        merged.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged["Content-Type"] = "application/json"
    raw = request_bytes(url, params=params, data=data, headers=merged, method=method)
    return json.loads(raw.decode("utf-8"))


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.current_row = None
        self.current_cell = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.current_row = []
        elif tag in ("td", "th") and self.current_row is not None:
            self.current_cell = []

    def handle_data(self, data):
        if self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.current_cell is not None:
            text = " ".join("".join(self.current_cell).split())
            self.current_row.append(text)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None
            self.current_cell = None


def as_int(value: str) -> int:
    match = re.search(r"-?\d+", value.replace(".", "").replace(",", ""))
    if not match:
        raise ValueError(f"Integer not found in {value!r}")
    return int(match.group())


def get_snt_branch_id() -> int:
    data = request_json(
        f"{BASE}/SearchBransKoduByProgramSeviye",
        params={"programSeviyeTipiAnahtari": PROGRAM_LEVEL},
    )
    for item in data:
        if str(item.get("dersBransKodu", "")).strip().upper() == BRANCH_CODE:
            return int(item["bransKoduId"])
    raise RuntimeError("SNT branch code was not found.")


def fetch_course() -> Dict[str, Any]:
    html = request_bytes(
        f"{BASE}/DersProgramSearch",
        params={
            "ProgramSeviyeTipiAnahtari": PROGRAM_LEVEL,
            "dersBransKoduId": get_snt_branch_id(),
        },
    ).decode("utf-8", errors="replace")

    parser = TableParser()
    parser.feed(html)

    for cells in parser.rows:
        if len(cells) < 11 or cells[0].strip() != CRN:
            continue

        course_code = cells[1].strip()
        if course_code != EXPECTED_COURSE_CODE:
            raise RuntimeError(
                f"CRN {CRN} belongs to {course_code!r}, expected "
                f"{EXPECTED_COURSE_CODE!r}. Monitoring stopped."
            )

        capacity = as_int(cells[9])
        enrolled = as_int(cells[10])

        return {
            "crn": CRN,
            "course_code": course_code,
            "course_title": cells[2].strip(),
            "day": cells[6].strip(),
            "time": cells[7].strip(),
            "capacity": capacity,
            "enrolled": enrolled,
            "available": max(capacity - enrolled, 0),
        }

    raise RuntimeError(f"CRN {CRN} / {EXPECTED_COURSE_CODE} was not found.")


def github_headers() -> Dict[str, str]:
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("GitHub Actions variables are missing.")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state() -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
    issues = request_json(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues",
        params={"state": "all", "per_page": 100},
        headers=github_headers(),
    )
    for issue in issues:
        if issue.get("title") == STATE_TITLE:
            number = int(issue["number"])
            try:
                return json.loads(issue.get("body") or "{}"), number
            except json.JSONDecodeError:
                return None, number
    return None, None


def save_state(state: Dict[str, Any], issue_number: Optional[int]) -> None:
    body = json.dumps(state, ensure_ascii=False, indent=2)
    if issue_number is None:
        created = request_json(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues",
            payload={"title": STATE_TITLE, "body": body},
            headers=github_headers(),
            method="POST",
        )
        issue_number = int(created["number"])
        request_json(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues/{issue_number}",
            payload={"state": "closed"},
            headers=github_headers(),
            method="PATCH",
        )
    else:
        request_json(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues/{issue_number}",
            payload={"body": body},
            headers=github_headers(),
            method="PATCH",
        )


def send_telegram(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets are not configured yet; notification skipped.")
        return False

    raw = request_bytes(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": "true",
        },
        method="POST",
    )
    result = json.loads(raw.decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return True


def main() -> None:
    current = fetch_course()
    previous, issue_number = load_state()

    print(
        f"Verified target: CRN={current['crn']} "
        f"{current['course_code']} - {current['course_title']} | "
        f"capacity={current['capacity']} enrolled={current['enrolled']} "
        f"available={current['available']}"
    )

    reasons = []
    if previous is None:
        if current["available"] > 0:
            reasons.append(f"Şu anda {current['available']} kişilik boş yer var")
    else:
        old_capacity = int(previous.get("capacity", 0))
        old_available = int(previous.get("available", 0))

        if current["capacity"] > old_capacity:
            reasons.append(
                f"Kontenjan arttı: {old_capacity} -> {current['capacity']}"
            )
        if old_available <= 0 and current["available"] > 0:
            reasons.append(
                f"Boş yer oluştu: {current['available']} kişilik yer var"
            )

    if reasons:
        message = (
            "🚨 İTÜ DERS KONTENJANI\n\n"
            f"CRN: {current['crn']}\n"
            f"Ders: {current['course_code']} — {current['course_title']}\n"
            f"Gün/Saat: {current['day']} {current['time']}\n"
            f"Kontenjan: {current['capacity']}\n"
            f"Kayıtlı: {current['enrolled']}\n"
            f"Boş yer: {current['available']}\n\n"
            + "\n".join(f"• {reason}" for reason in reasons)
            + "\n\nhttps://obs.itu.edu.tr/public/DersProgram"
        )
        if not send_telegram(message):
            # Preserve the previous state so the event remains pending until
            # Telegram secrets are configured.
            return
        print("Telegram notification sent.")

    save_state(current, issue_number)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
