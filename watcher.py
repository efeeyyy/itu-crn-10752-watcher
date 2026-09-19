import json
import os
import re
import sys
from typing import Any, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup

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

session = requests.Session()
session.headers.update({
    "User-Agent": "itu-crn-10752-watcher/1.0",
    "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
})


def as_int(value: str) -> int:
    match = re.search(r"-?\d+", value.replace(".", "").replace(",", ""))
    if not match:
        raise ValueError(f"Integer not found in {value!r}")
    return int(match.group())


def get_snt_branch_id() -> int:
    response = session.get(
        f"{BASE}/SearchBransKoduByProgramSeviye",
        params={"programSeviyeTipiAnahtari": PROGRAM_LEVEL},
        timeout=30,
    )
    response.raise_for_status()

    for item in response.json():
        if str(item.get("dersBransKodu", "")).strip().upper() == BRANCH_CODE:
            return int(item["bransKoduId"])

    raise RuntimeError("SNT branch code was not found.")


def fetch_course() -> Dict[str, Any]:
    branch_id = get_snt_branch_id()
    response = session.get(
        f"{BASE}/DersProgramSearch",
        params={
            "ProgramSeviyeTipiAnahtari": PROGRAM_LEVEL,
            "dersBransKoduId": branch_id,
        },
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 11 or cells[0].strip() != CRN:
            continue

        course_code = cells[1].strip()

        # Safety check: do not silently follow another course if CRNs change.
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
    response = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues",
        headers=github_headers(),
        params={"state": "all", "per_page": 100},
        timeout=30,
    )
    response.raise_for_status()

    for issue in response.json():
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
        response = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues",
            headers=github_headers(),
            json={"title": STATE_TITLE, "body": body},
            timeout=30,
        )
        response.raise_for_status()
        issue_number = int(response.json()["number"])

        response = requests.patch(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues/{issue_number}",
            headers=github_headers(),
            json={"state": "closed"},
            timeout=30,
        )
        response.raise_for_status()
    else:
        response = requests.patch(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues/{issue_number}",
            headers=github_headers(),
            json={"body": body},
            timeout=30,
        )
        response.raise_for_status()


def send_telegram(message: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets are not configured yet; notification skipped.")
        return False

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    response.raise_for_status()
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
            # Keep the old state so a pending event can still be delivered
            # after Telegram secrets are configured.
            if previous is not None or current["available"] > 0:
                return
        else:
            print("Telegram notification sent.")

    save_state(current, issue_number)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
