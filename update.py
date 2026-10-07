#!/usr/bin/env python3
"""Pull the DXE team's tasks from the ClickUp API and write site/data.json.

Runs in GitHub Actions. Needs the CLICKUP_TOKEN secret (a personal API token,
starts with "pk_"). CLICKUP_TEAM_ID is optional: when it is empty the script
uses the first workspace the token can see.

data.json "tasks" rows: [status, [person codes], created "YYYY-MM", closed "YYYY-MM" or "", size]
  status: t = To Do, p = In Progress, b = Blocked, c = Cancelled, d = Done
  size:   "major" | "medium" | "minor" | "" (from the task's tags)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.clickup.com/api/v2"
RIYADH = timezone(timedelta(hours=3))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "site", "data.json")
PEOPLE_PATH = os.path.join(ROOT, "people.json")

# ClickUp status name -> dashboard status
STATUS_MAP = {
    "to do": "t",
    "in progress": "p", "hands on": "p", "team review": "p", "stakeholders review": "p", "amends": "p",
    "blocked": "b",
    "cancelled": "c", "canceled": "c",
    "done": "d", "signed off": "d", "complete": "d", "completed": "d",
}
SIZES = ("major", "medium", "minor")


# ---------------------------------------------------------------- API helpers
def api_get(path, params=None, token=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    for attempt in range(6):
        req = urllib.request.Request(url, headers={"Authorization": token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or 0) or 15 * (attempt + 1)
                print(f"ClickUp {e.code}, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise SystemExit(f"ClickUp API error {e.code} for {path}: {e.read().decode()[:300]}")
    raise SystemExit("ClickUp kept refusing requests (rate limit). Try again later.")


def team_tasks(team_id, token, **filters):
    """All tasks in the workspace matching the filters (every page, subtasks included)."""
    out, page = [], 0
    while True:
        params = {"page": page, "subtasks": "true", **filters}
        data = api_get(f"/team/{team_id}/task", params, token)
        tasks = data.get("tasks", [])
        out.extend(tasks)
        if data.get("last_page", True) or not tasks:
            return out
        page += 1
        time.sleep(0.7)  # stay well under 100 requests per minute


def month_key(ms_value):
    return datetime.fromtimestamp(int(ms_value) / 1000, RIYADH).strftime("%Y-%m")


def status_of(task):
    st = task.get("status") or {}
    name = (st.get("status") or "").strip().lower()
    if name in STATUS_MAP:
        return STATUS_MAP[name]
    if "cancel" in name:
        return "c"
    kind = st.get("type")
    if kind in ("done", "closed"):
        return "d"
    if kind == "open":
        return "t"
    return "p"


def size_of(task):
    tags = {(t.get("name") or "").strip().lower() for t in task.get("tags") or []}
    for s in SIZES:
        if s in tags:
            return s
    return ""


# ---------------------------------------------------------------- main
def main():
    token = os.environ.get("CLICKUP_TOKEN", "").strip()
    if not token:
        raise SystemExit("CLICKUP_TOKEN secret is missing.")
    team_id = os.environ.get("CLICKUP_TEAM_ID", "").strip()
    if not team_id:
        teams = api_get("/team", token=token).get("teams", [])
        if not teams:
            raise SystemExit("The token cannot see any ClickUp workspace.")
        team_id = teams[0]["id"]
        print(f"Using workspace {teams[0].get('name')} ({team_id})")

    cfg = json.load(open(PEOPLE_PATH, encoding="utf-8"))
    members = cfg["members"]
    removed = cfg.get("removed", [])

    try:
        prev = json.load(open(DATA_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}

    now = datetime.now(RIYADH)
    year_start = datetime(now.year, 1, 1, tzinfo=RIYADH)
    year_start_ms = int(year_start.timestamp() * 1000)

    people = {}
    for code, v in (prev.get("people") or {}).items():
        people[code] = v

    def code_of(user):
        uid = str(user.get("id"))
        if uid in members:
            entry = members[uid]
            code, name, team = entry[0], entry[1], entry[2]
            joined = entry[3] if len(entry) > 3 else ""
            people[code] = [name, team, joined]
            return code
        code = "U" + uid
        name = user.get("username") or (user.get("email") or "").split("@")[0] or code
        people[code] = [name, "eg", ""]
        return code

    # Every task touched this year (covers created / finished / cancelled this year),
    # plus every task that is still open whatever its age.
    touched = team_tasks(team_id, token, include_closed="true", date_updated_gt=year_start_ms - 1)
    open_now = team_tasks(team_id, token)  # open tasks only (closed excluded by default)
    by_id = {t["id"]: t for t in touched}
    for t in open_now:
        by_id.setdefault(t["id"], t)

    rows = []
    for t in by_id.values():
        assignees = t.get("assignees") or []
        if not assignees:
            continue
        st = status_of(t)
        created = month_key(t["date_created"]) if t.get("date_created") else ""
        closed = ""
        if st in ("d", "c"):
            when = t.get("date_done") or t.get("date_closed")
            if not when:
                continue
            closed = month_key(when)
            if int(when) < year_start_ms:
                continue  # finished before this year
        rows.append([st, [code_of(a) for a in assignees], created, closed, size_of(t)])

    data = {
        "generatedAt": int(now.timestamp() * 1000),
        "year": str(now.year),
        "removed": removed,
        "newJoinerDays": cfg.get("newJoinerDays", 90),
        "people": people,
        "tasks": rows,
    }
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    counts = {k: sum(1 for r in rows if r[0] == k) for k in "tpbcd"}
    print(f"tasks {len(rows)} | to do {counts['t']} | in progress {counts['p']} | blocked {counts['b']} "
          f"| cancelled {counts['c']} | done {counts['d']}")


if __name__ == "__main__":
    main()
