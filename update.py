#!/usr/bin/env python3
"""Pull the DXE team's tasks from the ClickUp API and write site/data.json.

Runs in GitHub Actions twice a day. Needs the CLICKUP_TOKEN secret (a personal
API token, starts with "pk_"). CLICKUP_TEAM_ID is optional: when it is empty the
script uses the first workspace the token can see.
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

OPEN = ["to do", "blocked", "in progress", "hands on", "team review"]
STAKEHOLDERS = ["stakeholders review"]
OUT = ["stakeholders review", "done", "signed off"]          # summary card + "done this week"
FINISHED = ["done", "signed off", "complete"]                # monthly "Closed" / "Done"
HISTORY_DAYS = 35


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
        params = {"page": page, "subtasks": "true", "include_closed": "true", **filters}
        data = api_get(f"/team/{team_id}/task", params, token)
        tasks = data.get("tasks", [])
        out.extend(tasks)
        if data.get("last_page", True) or not tasks:
            return out
        page += 1
        time.sleep(0.7)  # stay well under 100 requests per minute


# ---------------------------------------------------------------- date helpers
def ms(dt):
    return int(dt.timestamp() * 1000)


def riyadh(ms_value):
    return datetime.fromtimestamp(int(ms_value) / 1000, RIYADH)


def month_key(ms_value):
    return riyadh(ms_value).strftime("%Y-%m")


def done_ms(task):
    return task.get("date_done") or task.get("date_closed")


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
    ksa_ids = {uid for uid, v in members.items() if v[2] == "ksa"}

    try:
        prev = json.load(open(DATA_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}

    now = datetime.now(RIYADH)
    year = now.year
    year_start = datetime(year, 1, 1, tzinfo=RIYADH)
    week_start = (now - timedelta(days=(now.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    today_key = now.strftime("%Y-%m-%d")

    # people: keep earlier entries, refresh names and teams from people.json
    people = dict(prev.get("people", {}))

    def code_of(user):
        uid = str(user.get("id"))
        if uid in members:
            code, name, team = members[uid]
            people[code] = [name, team]
            return code
        code = "U" + uid
        name = user.get("username") or (user.get("email") or "").split("@")[0] or code
        people[code] = [name, "eg"]
        return code

    def codes(task):
        return [code_of(a) for a in task.get("assignees") or []]

    def is_ksa(task):
        return any(str(a.get("id")) in ksa_ids for a in task.get("assignees") or [])

    def status(task):
        return (task.get("status") or {}).get("status", "").lower()

    # 1. what is open right now (any creation date)
    current = team_tasks(team_id, token, **{"statuses[]": OPEN + STAKEHOLDERS})
    open_rows, sr_rows, cm = [], [], {}
    now_ms = ms(now)
    for t in current:
        cs = codes(t)
        if not cs:
            continue
        st = status(t)
        cm[t["id"]] = month_key(t["date_created"])
        if st in STAKEHOLDERS:
            sr_rows.append(cs)
            continue
        if st not in OPEN:
            continue
        stage = {"to do": "t", "blocked": "b", "team review": "r"}.get(st, "p")
        due = t.get("due_date")
        overdue = 1 if due and int(due) < now_ms else 0
        open_rows.append([stage, cs, overdue, t["id"]])

    # 2. tasks created this year -> came in per month and year totals
    created = team_tasks(team_id, token, date_created_gt=ms(year_start) - 1)
    in_m = {}
    for t in created:
        m = in_m.setdefault(month_key(t["date_created"]), {"n": 0, "ksa": 0})
        m["n"] += 1
        m["ksa"] += 1 if is_ksa(t) else 0
    year_in = {"all": sum(v["n"] for v in in_m.values()), "ksa": sum(v["ksa"] for v in in_m.values())}

    # 3. finished this year (done / signed off / complete) -> per month and per person
    finished = team_tasks(team_id, token, **{"statuses[]": FINISHED, "date_done_gt": ms(year_start) - 1})
    done_m = {}
    for t in finished:
        cs = codes(t)
        when = done_ms(t)
        if not cs or not when:
            continue
        m = done_m.setdefault(month_key(when), {"n": 0, "eg": 0, "ksa": 0, "by": {}})
        m["n"] += 1
        teams = {people[c][1] for c in cs}
        m["eg"] += 1 if "eg" in teams else 0
        m["ksa"] += 1 if "ksa" in teams else 0
        for c in cs:
            m["by"][c] = m["by"].get(c, 0) + 1

    # 4. reached stakeholders review / done / signed off this year (summary card) and this week
    out_year = team_tasks(team_id, token, **{"statuses[]": OUT, "date_done_gt": ms(year_start) - 1})
    year_done = {"all": len(out_year), "ksa": sum(1 for t in out_year if is_ksa(t))}
    week_ms = ms(week_start)
    done_week = [codes(t) for t in out_year if done_ms(t) and int(done_ms(t)) >= week_ms and t.get("assignees")]

    # 5. daily history (kept from earlier runs)
    history = {k: v for k, v in prev.get("history", {}).items()
               if k >= (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")}
    today = {}
    for stage, cs, _, _ in open_rows:
        if stage in ("p", "r", "b"):
            for c in cs:
                today[c] = today.get(c, 0) + 1
    history[today_key] = today

    data = {
        "generatedAt": now_ms,
        "weekStart": week_ms,
        "capacity": prev.get("capacity", 8),
        "overdueTracked": True,
        "blockedTracked": True,
        "inExact": True,
        "removed": removed,
        "people": people,
        "open": open_rows,
        "sr": sr_rows,
        "done": done_week,
        "doneM": dict(sorted(done_m.items())),
        "inM": dict(sorted(in_m.items())),
        "cm": cm,
        "year": {"label": str(year), "in": year_in, "done": year_done},
        "history": dict(sorted(history.items())),
        "historySince": prev.get("historySince") or today_key,
    }
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"open {len(open_rows)}, stakeholders review {len(sr_rows)}, came in {year_in['all']}, "
          f"reached review/done {year_done['all']}, finished {sum(v['n'] for v in done_m.values())}")


if __name__ == "__main__":
    main()
