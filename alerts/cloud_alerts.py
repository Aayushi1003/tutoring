#!/usr/bin/env python3
"""Send the tutor's class alerts to their phone, run by GitHub Actions every 15 minutes.

Reads the timetable from Firestore (users/<uid>/data/planner), works out what is due in
the next ~75 minutes, and hands each alert to ntfy with an exact delivery time ("At"),
so alerts land on time even when GitHub starts the run late. Alerts already handed to
ntfy are remembered in users/<uid>/data/alerts_sent, so nothing is sent twice.

Needs these environment variables (GitHub repository secrets):
  FIREBASE_SERVICE_ACCOUNT  the Firebase service-account key (JSON text)
  TUTOR_UID                 the tutor's Firebase user id
  NTFY_TOPIC                the private ntfy topic the iPhone subscribes to
Run locally with TEST_NOW="YYYY-MM-DD HH:MM" and --dry to print instead of sending.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta

LOOKAHEAD = 75   # minutes: covers the gap between runs, with room for GitHub's delays
LATE_OK = 5      # minutes: still send an alert whose time has just passed


def to_min(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def fmt12(m):
    h, mm = divmod(m, 60)
    ap = "PM" if h % 24 >= 12 else "AM"
    h = h % 12 or 12
    return f"{h}:{mm:02d} {ap}" if mm else f"{h} {ap}"


def dur(m):
    h, r = divmod(m, 60)
    return (f"{h}h {r}m" if r else f"{h}h") if h else f"{r}m"


def label(s):
    return " · ".join(p for p in (s.get("student"), s.get("subject")) if p) or "Class"


def plan(state, day):
    """The day's classes and free gaps, worked out the same way as the app."""
    d = day.strftime("%Y-%m-%d")
    wd = (day.weekday() + 1) % 7  # the app uses 0 = Sunday
    sessions = []
    for s in state.get("sessions", []):
        if d in (s.get("cancelled") or []):
            continue
        if s.get("date"):
            if s["date"] != d:
                continue
        elif wd not in (s.get("days") or []):
            continue
        sessions.append({"s": s, "start": to_min(s["start"]), "end": to_min(s["end"])})
    routine = [{"start": to_min(r["start"]), "end": to_min(r["end"])}
               for r in state.get("routine", []) if wd in r.get("days", [])]
    busy = sorted(sessions + routine, key=lambda x: (x["start"], x["end"]))
    st = state.get("settings", {})
    ds, de = to_min(st.get("dayStart", "08:00")), to_min(st.get("dayEnd", "21:30"))
    min_free = int(st.get("minFree", 30) or 30)
    free, cur = [], ds
    for x in busy:
        if x["start"] > cur and min(x["start"], de) - cur >= min_free:
            free.append((cur, min(x["start"], de)))
        cur = max(cur, x["end"])
    if de - cur >= min_free:
        free.append((cur, de))
    sessions.sort(key=lambda x: x["start"])
    return sessions, free


def earnings(state, sessions):
    total = 0
    for x in sessions:
        f = (state.get("fees") or {}).get(x["s"].get("student"))
        if not f:
            continue
        amt = float(f.get("amount") or 0)
        if f.get("per") == "hour":
            total += (x["end"] - x["start"]) / 60 * amt
        elif f.get("per") == "class":
            total += amt
    return total


def due_alerts(state, now):
    """(key, minute of day to deliver, text) for everything due in the next LOOKAHEAD minutes."""
    today = now.strftime("%Y-%m-%d")
    now_min = now.hour * 60 + now.minute
    st = state.get("settings") or {}
    cur = st.get("currency", "₹")
    remind = int(st.get("remindMin", 15) or 15)
    sessions, free = plan(state, now)
    out = []

    def window(at):
        return now_min - LATE_OK <= at <= now_min + LOOKAHEAD

    # Morning plan: delivered at 7 AM, or at the first run after that (GitHub can start late).
    morning = 7 * 60
    if morning - LOOKAHEAD <= now_min < 12 * 60:
        if sessions:
            listing = ", ".join(f"{x['s'].get('student') or 'Class'} {fmt12(x['start'])}" for x in sessions)
            earn = earnings(state, sessions)
            text = f"Today: {len(sessions)} class{'es' if len(sessions) != 1 else ''}" + \
                   (f", {cur}{round(earn):,}" if earn else "") + f". {listing}."
        else:
            text = "Today: no classes. Enjoy the free day!"
        out.append((f"{today}|morning", max(morning, now_min), text))

    for x in sessions:
        at = x["start"] - remind
        if window(at):
            s = x["s"]
            where = f" · {s['place']}" if s.get("place") else ""
            out.append((f"{today}|class|{s['id']}|{x['start']}", at,
                        f"{label(s)} at {fmt12(x['start'])}, in {dur(remind)}{where}"))

    for a, b in free:
        if window(a) and b - a >= 15:
            nxt = next((x for x in sessions if x["start"] >= b), None)
            then = f", then {label(nxt['s'])}" if nxt else ""
            out.append((f"{today}|free|{a}", a, f"Free time until {fmt12(b)} ({dur(b - a)}){then}."))
    return out


def ntfy(topic, text, deliver_at=None):
    headers = {"Title": "Tutoring", "Tags": "books"}
    if deliver_at:
        headers["At"] = str(int(deliver_at.timestamp()))
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return 200 <= r.status < 300


def main():
    dry = "--dry" in sys.argv
    now = datetime.strptime(os.environ["TEST_NOW"], "%Y-%m-%d %H:%M") if os.environ.get("TEST_NOW") else datetime.now()

    if dry and len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        state = json.load(open(sys.argv[1])).get("state")
        sent, save = {}, None
    else:
        missing = [k for k in ("FIREBASE_SERVICE_ACCOUNT", "TUTOR_UID", "NTFY_TOPIC") if not os.environ.get(k)]
        if missing:  # not set up yet: skip quietly instead of failing every 15 minutes
            print("Alerts not set up yet; add these repository secrets: " + ", ".join(missing))
            return
        from google.cloud import firestore
        from google.oauth2 import service_account
        info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
        db = firestore.Client(project=info["project_id"], credentials=service_account.Credentials.from_service_account_info(info))
        user = db.collection("users").document(os.environ["TUTOR_UID"])
        snap = user.collection("data").document("planner").get()
        state = (snap.to_dict() or {}).get("state") if snap.exists else None
        sent_ref = user.collection("data").document("alerts_sent")
        sent_snap = sent_ref.get()
        sent = (sent_snap.to_dict() or {}).get("keys", {}) if sent_snap.exists else {}
        save = lambda keys: sent_ref.set({"keys": keys, "updatedAt": int(datetime.now().timestamp() * 1000)})

    if not state:
        print("No timetable saved yet; nothing to do.")
        return

    keep_from = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    sent = {k: v for k, v in sent.items() if v >= keep_from}
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    for key, at, text in due_alerts(state, now):
        if key in sent:
            continue
        deliver = midnight + timedelta(minutes=at)
        when = deliver if deliver > now + timedelta(minutes=1) else None
        if dry:
            print(f"[dry] {deliver:%H:%M} {text}")
            continue
        try:
            if ntfy(os.environ["NTFY_TOPIC"], text[:200], when):
                sent[key] = now.strftime("%Y-%m-%d")
                count += 1
        except Exception as e:  # leave it unsent; the next run tries again
            print(f"failed: {key}: {e}")
    if save:
        save(sent)
    print(f"{now:%Y-%m-%d %H:%M} handed {count} alert(s) to ntfy")


if __name__ == "__main__":
    main()
