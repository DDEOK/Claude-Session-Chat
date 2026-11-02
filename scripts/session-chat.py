#!/usr/bin/env python3
"""A thread-based channel for Claude Code sessions on the same machine to ask each other for information and work.

**Delivery is done by the mod.** In every session the plugin's mod calls `poll` every few seconds, picks up posts addressed to that session, and opens a new turn with them when the session is idle. Where there is no mod (other agents), `wait` can run in the background instead.

**Who is who.** Sessions address each other by a fixed ID: the first 8 characters of `CLAUDE_CODE_SESSION_ID` (the mod passes it with `--id`). A subagent uses `--sub <label>`, giving `<ID>.<label>`. People see the name each session gives itself and the thread titles.

**Where**: one folder, `~/.cache/session-chat/`. It is local state of this machine and never goes into a repository.

- `sessions.json`: session profiles. Name and current work (written by the session), folder, repository, branch, recent files (written by the mod), avatar.
- `alive/<ID>`: heartbeat. The mod refreshes it every few seconds; sessions that stop are dropped from the list.
- `log.jsonl`: every post in time order. Append only.
- `threads.json`: thread title, opener, participants. State is computed from the last reply in the log.
- `board.json`: who has claimed what.
- `cursors/<ID>`: how far that participant has read.

The rules sessions follow live in `skills/session-chat/SKILL.md`. Runs on macOS, Linux and Windows (locking with `fcntl`, or `msvcrt` on Windows). All files are read and written as UTF-8.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

ROOT = os.path.expanduser("~/.cache/session-chat")
POLL = 1.0
LIVE = 30
# 살아 있어도 이만큼(초) 사람 요청이나 턴이 없으면 "쉬는 중"으로 본다. 생존 신호는 모드가 계속 보내므로 쓰이는지와는 따로 잰다
IDLE = 1800
# "처리 중" 표시가 이만큼(초) 지나도 글도 턴 끝도 없으면 내린다. 멈춘 세션이 "답을 쓰는 중"으로 영영 남지 않게 하려는 것이다
BUSY_MAX = 900
# 멘션 없는 사용자 질문에 이만큼(초) 안에 아무도 손을 들지 않으면 "아무도 답하지 않음"으로 보인다
OPEN_WAIT = 600
KINDS = ("info", "ask", "request", "reply", "comment")
USER = "user"
RECENT = 20
# 아바타는 avatars/<모양>.svg의 #COLOR 자리에 색을 넣어 그린다. 세션 ID로 정해져 같은 세션은 늘 같은 그림이다.
SHAPES = ("terminal", "bolt", "leaf", "book", "gear", "star", "wave", "cube")
COLORS = {"coral": "#E0644A", "amber": "#D99A20", "olive": "#7A9A3A", "teal": "#2A9D8F", "sky": "#3A86C8", "indigo": "#5B5BD6", "plum": "#A04FB0", "slate": "#5F6B7A"}
ID = re.compile(r"^[a-z0-9]{4,16}(\.[a-z0-9-]{1,24})?$")


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _path(*parts):
    p = os.path.join(ROOT, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _me(a):
    sid = a.id or os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8]
    if not sid:
        sys.exit("Unknown session ID. Pass --id where CLAUDE_CODE_SESSION_ID is not set.")
    if getattr(a, "sub", None):
        return f"{sid}.{a.sub}"
    return sid


class _Lock:
    """한 곳의 잠금 파일 하나로 쓰기를 직렬화한다."""

    def __enter__(self):
        self.f = open(_path("lock"), "a+")
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_EX)
        else:
            self.f.seek(0)
            while True:
                try:
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)

    def __exit__(self, *_):
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        else:
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        self.f.close()


def _load(name, default):
    try:
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(name, data):
    tmp = _path(name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(name))


def _log():
    try:
        with open(os.path.join(ROOT, "log.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.endswith("\n")]
    except FileNotFoundError:
        return []


def _beat(sid):
    with open(_path("alive", sid), "w", encoding="utf-8") as f:
        f.write(str(time.time()))


def _is_live(sid):
    try:
        return time.time() - os.path.getmtime(os.path.join(ROOT, "alive", sid.split(".")[0])) < LIVE
    except OSError:
        return False


def _live_sessions():
    return {sid: p for sid, p in _load("sessions.json", {}).items() if _is_live(sid)}


def _label(sessions, pid):
    if pid == USER:
        return "user"
    base, _, sub = pid.partition(".")
    title = sessions.get(base, {}).get("title") or base
    return f"{title}·{sub}" if sub else title


def _pick_avatar(sid, sessions, shape=None, color=None):
    """세션 ID에서 정해지는 모양·색 조합 중 다른 세션이 아직 안 쓴 첫 조합을 고른다. 지정한 값은 그대로 쓴다."""
    used = {(p["avatar"]["shape"], p["avatar"]["color"]) for s, p in sessions.items() if s != sid and "avatar" in p}
    combos = [(s, c) for s in SHAPES for c in COLORS]
    start = int(hashlib.sha1(sid.encode()).hexdigest(), 16) % len(combos)
    for i in range(len(combos)):
        s, c = combos[(start + i * 13) % len(combos)]
        if (shape and s != shape) or (color and c != color):
            continue
        if (s, c) not in used or i == len(combos) - 1:
            return {"shape": s, "color": c}
    return {"shape": shape or SHAPES[0], "color": color or "slate"}


def _git(cwd, *args):
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _words(text):
    return {w for w in re.findall(r"[\w.]{2,}", (text or "").lower())}


def _overlaps(sid, sessions):
    """하는 일이 겹칠 수 있는 다른 살아 있는 세션. 같은 저장소에서 최근 파일이 겹치거나 하는 일 설명이 비슷하면."""
    me = sessions.get(sid)
    out = []
    if not me:
        return out
    mine = {r["path"] for r in me.get("recent", [])}
    for other, p in sessions.items():
        if other == sid or not _is_live(other):
            continue
        same_place = (me.get("repo") and me.get("repo") == p.get("repo")) or me.get("cwd") == p.get("cwd")
        files = mine & {r["path"] for r in p.get("recent", [])}
        a, b = _words(me.get("doing")), _words(p.get("doing"))
        similar = a and b and len(a & b) / max(1, min(len(a), len(b))) >= 0.5
        if same_place and (files or similar):
            out.append({"id": other, "title": p.get("title", ""), "files": sorted(files)[:5], "similar": bool(similar)})
    return out


def cmd_profile(a):
    """프로필을 고친다. 이름·하는 일은 세션이, 폴더·저장소·브랜치는 모드가 준다. 겹칠 수 있는 세션을 돌려준다."""
    sid = _me(a)
    with _Lock():
        sessions = _load("sessions.json", {})
        p = sessions.setdefault(sid, {"joined": _now()})
        p.pop("left", None)
        # 모드의 자동 요약(--auto)은 세션이 10분 안에 직접 고친 이름·하는 일을 덮어쓰지 않는다
        manual_recent = time.time() - p.get("manual_at", 0) < 600
        # 앱이 붙인 제목을 그대로 쓰는 세션(--app-title)은 자동 요약이 이름을 바꾸지 않는다. 사이드바와 패널의 이름이 갈리지 않게 하려는 것이다
        keep_title = a.auto and p.get("title_from_app")
        for key in ("title", "doing"):
            if getattr(a, key) and not (a.auto and manual_recent) and not (key == "title" and keep_title):
                p[key] = getattr(a, key)
        if a.title and not a.auto:
            p["title_from_app"] = bool(a.app_title)
        if (a.title or a.doing) and not a.auto:
            p["manual_at"] = time.time()
        for key in ("cwd", "host", "parent", "group"):
            if getattr(a, key):
                p[key] = getattr(a, key)
        if a.cwd:
            p["repo"] = _git(a.cwd, "rev-parse", "--show-toplevel")
            p["branch"] = _git(a.cwd, "rev-parse", "--abbrev-ref", "HEAD")
        if a.avatar or a.color or "avatar" not in p:
            p["avatar"] = _pick_avatar(sid, sessions, a.avatar, a.color)
        p["updated"] = _now()
        _save("sessions.json", sessions)
        # 참여한 순간부터 받게 읽은 위치를 지금 끝에 둔다. 첫 poll 전에 온 글을 놓치지 않으려는 것이다
        if _cursor(sid) is None:
            _set_cursor(sid, os.path.getsize(_path("log.jsonl")) if os.path.exists(_path("log.jsonl")) else 0)
    _beat(sid)
    print(json.dumps({"id": sid, "title": p.get("title", ""), "doing": p.get("doing", ""), "overlaps": _overlaps(sid, sessions)}, ensure_ascii=False))


def _idle(p):
    """마지막 활동(사람 요청·턴) 뒤로 지난 초. 기록이 없으면 참여 시각부터 잰다."""
    last = p.get("last_active")
    if last is None:
        try:
            last = datetime.datetime.fromisoformat(p.get("joined", "")).timestamp()
        except ValueError:
            return 0
    return max(0, time.time() - last)


# 시간 표기 언어. 패널(dump)은 사용자 언어를 --lang으로 주고, 세션이 읽는 출력은 영어로 둔다
LANG = "en"


def _ago(seconds):
    m = int(seconds // 60)
    ko = LANG == "ko"
    if m < 60:
        return f"{m}분" if ko else f"{m}m"
    h, m = divmod(m, 60)
    if h >= 24:
        return f"{h // 24}일" if ko else f"{h // 24}d"
    if ko:
        return f"{h}시간 {m}분" if m else f"{h}시간"
    return f"{h}h {m}m" if m else f"{h}h"


def cmd_activity(a):
    """모드가 사람 요청이나 턴이 있을 때 부른다. 생존 신호와 달리 세션이 실제로 쓰이고 있다는 표시다."""
    sid = _me(a)
    back = False
    with _Lock():
        sessions = _load("sessions.json", {})
        if sid in sessions:
            sessions[sid]["last_active"] = time.time()
            # 대화방을 나간 세션도 사용자가 그 창에 요청을 보내면 다시 참여한다. 나간 세션은 다른 세션이 불러도 못 받아서,
            # 스레드에서 나가라는 말을 대화방 나가기로 잘못 알아들은 세션 셋이 그대로 끊긴 적이 있다(2026-11-03)
            if a.user and sessions[sid].pop("left", None):
                back = True
            _save("sessions.json", sessions)
    if back:
        _beat(sid)
        print("Back in the session chat.")


def cmd_touch(a):
    """모드가 세션이 고친 파일을 최근 파일에 올린다."""
    sid = _me(a)
    path = os.path.abspath(a.path)
    with _Lock():
        sessions = _load("sessions.json", {})
        p = sessions.setdefault(sid, {"joined": _now()})
        recent = [r for r in p.get("recent", []) if r["path"] != path]
        p["recent"] = [{"path": path, "ts": _now()}, *recent][:RECENT]
        _save("sessions.json", sessions)


def cmd_check(a):
    """파일을 고치기 전에 겹침을 본다. 남이 맡았으면 claimed, 남이 최근 고쳤으면 recent."""
    sid = _me(a).split(".")[0]
    path = os.path.abspath(a.path)
    sessions = _live_sessions()
    claim = _load("board.json", {}).get(path)
    out = {"claimed": None, "recent": []}
    if claim and claim["by"].split(".")[0] != sid and _is_live(claim["by"]):
        out["claimed"] = {"id": claim["by"], "title": _label(sessions, claim["by"]), "note": claim.get("note", "")}
    for other, p in sessions.items():
        if other != sid and any(r["path"] == path for r in p.get("recent", [])):
            out["recent"].append({"id": other, "title": p.get("title", "")})
    print(json.dumps(out, ensure_ascii=False))


def cmd_find(a):
    """Pick sessions to ask, with reasons. Scores claims, then recent files, then same repository, then words in current work."""
    me = _me(a).split(".")[0]
    q = a.query
    qpath = os.path.abspath(q) if os.sep in q or "." in q else None
    words = _words(q)
    board = _load("board.json", {})
    out = []
    for sid, p in _live_sessions().items():
        if sid == me:
            continue
        score, why = 0, []
        for target, c in board.items():
            if c["by"].split(".")[0] == sid and (target == qpath or q.lower() in target.lower()):
                score += 100
                why.append(f"claimed: {target}")
        for r in p.get("recent", []):
            if r["path"] == qpath or (len(q) > 2 and q.lower() in r["path"].lower()):
                score += 50
                why.append(f"recently edited: {r['path']}")
                break
        if qpath and p.get("repo") and qpath.startswith(p["repo"] + os.sep):
            score += 20
            why.append(f"same repository: {p['repo']}")
        hit = words & (_words(p.get("doing")) | _words(p.get("title")))
        if hit:
            score += 10 * len(hit)
            why.append("current work: " + ", ".join(sorted(hit)))
        idle = _idle(p)
        if score and idle > IDLE:
            # 오래 쉰 세션은 답이 늦고, 받는 턴이 캐시가 만료된 대화를 다시 처리해 비싸다
            score = max(1, score - 30)
            why.append(f"idle for {_ago(idle)}, may answer late")
        if score:
            out.append({"id": sid, "title": p.get("title", ""), "doing": p.get("doing", ""), "score": score, "why": why})
    out.sort(key=lambda x: -x["score"])
    print(json.dumps(out[:5], ensure_ascii=False, indent=1) if out else "No candidates. Use who for the full list, or ask the user whom to ask.")


def cmd_who(a):
    sessions = _live_sessions()
    if a.json:
        print(json.dumps(sessions, ensure_ascii=False))
        return
    if not sessions:
        print("No live sessions")
    for sid, p in sorted(sessions.items()):
        idle = _idle(p)
        rest = f" · idle for {_ago(idle)}" if idle > IDLE else ""
        print(f"{sid} · {p.get('title') or '(no name)'} · {p.get('doing', '')} · {p.get('repo') or p.get('cwd', '')}{rest}")


def _resolve(to, sessions):
    if to == USER or to in sessions or to.split(".")[0] in sessions:
        return to
    hits = [sid for sid, p in sessions.items() if p.get("title") == to]
    if len(hits) == 1:
        return hits[0]
    sys.exit(f"'{to}' is not a known session, or it quit the session chat. Find the ID with who or find.")


def _wake(me, kind, status, to, replied):
    """받는 세션 중 이 글로 새 턴을 여는 세션. 나머지는 턴 없이 대화 기록에만 받는다.

    세션들이 자원을 잡고 놓을 때마다 진행 글(info)로 서로를 멘션해, 할 일이 없는 세션마다 턴이 열려 "확인했다"고만 답하는 일이 반복됐다(2026-11-03 사용자 지적).
    그래서 할 일이 생기는 글만 깨운다: 사용자 글, 질문·요청, 물어본 세션에게 가는 끝난 답(answered·declined). 진행 글과 작업 중 답(partial)은 깨우지 않는다.
    """
    if me == USER or kind in ("ask", "request", "comment"):
        return list(to)
    if kind == "reply" and status != "partial" and replied:
        return [t for t in to if t == replied["from"]]
    return []


def _check_reply_target(me, kind, replied):
    """세션 글이 답글로 달릴 수 있는 자리인지 본다. 사용자 의견과 스레드의 새 글(--top)은 보지 않는다.

    규칙이 없던 때 한 스레드의 글 40개가 거의 모두 첫 질문의 답글로 붙었다. 자원 알림, 새 질문, 자기 첫 글에 단 덧붙임까지였다(2026-11-03 사용자 지적).
    답은 질문·요청·사용자 의견에만 달고, 답에 이어 물을 것은 그 답에 질문(ask)으로 단다. 알림과 새로 시작하는 말은 누구의 답도 아니라 새 글로 올린다.
    """
    if replied["from"] == me:
        sys.exit("You cannot reply to your own post. To add to it or bring up something new in this thread, post with --top (a new post in the thread).")
    if kind == "info":
        sys.exit("An info post answers nothing, so it is not a reply. Post it with --top (a new post in the thread), or skip it if nobody has to act on it.")
    if kind == "reply" and replied["kind"] not in ("ask", "request", "comment"):
        sys.exit(f"A reply answers a question or request, and post {replied['id']} is a {replied['kind']}. To follow up on it, post --kind ask --reply-to {replied['id']}; to say something new, post with --top.")


def cmd_post(a):
    """Post a message. Recipients are the mentions (--to); only mentioned sessions receive it.

    - In a reply or comment, an empty --to mentions the author of the post being answered, so a forgotten mention never loses the answer.
    - Only the user may ask without a mention (--open). Such a post goes to every live session in the thread, and only the first to take it answers.
    """
    me = _me(a)
    live = _live_sessions()
    # 받는 세션은 살아 있지 않아도 된다. 꺼진 세션은 다시 켜지면 그사이 온 글을 받는다. 중단된 작업을 다시 살릴 수 있어야 해서 막지 않는다(2026-11-03 사용자 결정)
    sessions = {sid: p for sid, p in _load("sessions.json", {}).items() if not p.get("left")} | live
    log = _log()
    replied = next((m for m in log if m["id"] == a.reply_to), None) if a.reply_to else None
    if a.reply_to and not replied:
        sys.exit(f"Post '{a.reply_to}' not found.")
    threads = _load("threads.json", {})
    t_meta = threads.get(replied["thread"], {}) if replied else {}
    if a.open:
        if me != USER:
            sys.exit("Only the user may ask without a mention (--open). Sessions mention recipients with --to.")
        # 멘션 없는 질문은 먼저 손 든 세션이 답하므로 지금 살아 있는 세션에게만 간다
        names = [p for p in t_meta.get("participants", []) if p.split(".")[0] in live]
    else:
        names = [t for t in (a.to or "").split(",") if t]
        if not names and replied:
            # 멘션을 비운 답은 답하는 글의 작성자에게 간다. 그 세션이 스레드에서 나갔으면 아무에게도 보내지 않고 스레드에만 남긴다
            names = [replied["from"]] if replied["from"] not in t_meta.get("parted", {}) else []
            if not names:
                print(f"Note: {_label(sessions, replied['from'])} has left this thread, so this post stays in the thread and wakes nobody.", file=sys.stderr)
    # 사용자 글은 대화방을 나간 세션에는 보내지 않는다. 떠난 참여자 하나 때문에 글 전체가 막히지 않게 한다. 꺼졌을 뿐인 세션은 받는다
    if me == USER:
        names = [t for t in names if t.split(".")[0] in sessions]
    to = [_resolve(t, sessions) for t in names if t != me]
    if not to and not replied:
        sys.exit("No recipient. Give a session ID to mention with --to (find it with who or find).")
    if not to and me == USER:
        sys.exit("No recipient. Give a session ID to mention with --to (find it with who or find).")
    strangers = [t for t in to if t_meta and t != USER and t not in t_meta.get("participants", [])]
    if strangers:
        print(f"Note: {', '.join(_label(sessions, s) for s in strangers)} was not in this thread. Check the mention is right.", file=sys.stderr)
    # 종류를 안 적으면 받은 글에 이어 쓰는 것은 답(reply)으로 본다. 기본값이 질문이던 때 답이 "질문"으로 올라가 상대가 또 답할 차례로 잡혔다
    if not a.kind:
        a.kind = "comment" if me == USER and replied else ("reply" if replied else "ask")
    if replied and me != USER and not a.top:
        _check_reply_target(me, a.kind, replied)
    if a.top and me != USER and a.kind == "reply":
        sys.exit("A new post (--top) answers nothing, so it is not a reply. Give --kind info, ask or request.")
    msg = {"id": uuid.uuid4().hex[:8], "ts": _now(), "from": me, "to": to, "kind": a.kind, "text": a.text}
    msg["wake"] = _wake(me, a.kind, a.status, to, replied)
    if a.sig:
        msg["sig"] = a.sig
    if a.open:
        msg["open"] = True
    # 새 글 표시: 스레드에 이어 쓴 글이지만 첫 글의 답글이 아니라 스레드의 새 글이다. 패널은 첫 글처럼 카드 없이 그린다
    if a.top:
        msg["top"] = True
    with _Lock():
        threads = _load("threads.json", {})
        if replied:
            msg["thread"] = replied["thread"]
            msg["reply_to"] = replied["id"]
            # 멘션 없는 사용자 질문은 손 든 세션만 답한다. 손을 안 들고 바로 답해도 이 자리에서 손들기를 대신한다.
            # 잠금 안이라 먼저 올린 하나만 성공하고, 늦게 온 세션은 같은 답을 또 올리지 못한다(둘 다 답한 실측이 있다)
            if replied.get("open") and me != USER:
                takes = _load("takes.json", {})
                taker = takes.get(replied["id"])
                if taker and taker != me and _is_live(taker):
                    sys.exit(f"{_label(sessions, taker)} is already answering this question. Post nothing.")
                takes[replied["id"]] = me
                _save("takes.json", takes)
            if a.kind == "reply":
                # 기본값을 두지 않는다. 기본이 "끝남"이면 답 하나로 진행 중인 스레드가 끝난 것으로 보였다
                if not a.status:
                    sys.exit("A reply needs --status: answered when the request is done, partial while still working, declined when you will not do it.")
                msg["status"] = a.status
            t = threads.setdefault(msg["thread"], {"title": "", "by": replied["from"], "participants": [replied["from"], *replied["to"]], "created": replied["ts"]})
        else:
            if a.kind in ("reply", "comment"):
                sys.exit(f"{a.kind} needs --reply-to <post id>.")
            if not a.title:
                sys.exit("A new thread needs --title: a name that tells the user what it is about.")
            # 같은 상대와 열린 스레드가 있으면 알려 준다. 막지는 않는다(주제가 다를 수 있다). 짧은 스레드가 상대마다 계속 새로 생기는 것을 줄이려는 것이다(2026-11-02 사용자 결정)
            if me != USER:
                same = [s for s in _thread_states().values() if not s["closed"] and me in s["participants"] and all(x in s["participants"] for x in to)]
                for s in same[:3]:
                    print(f"Note: thread '{s['title']}' ({s['id']}) is still open with the same sessions. If this is the same topic, post there with --reply-to {s['root']['id']} instead of a new thread.", file=sys.stderr)
            msg["thread"] = msg["id"]
            t = threads[msg["id"]] = {"title": a.title, "by": me, "participants": [], "created": msg["ts"]}
        # 글을 쓰거나 멘션되면 참여자가 된다. 나갔던 세션도 다시 멘션되면 돌아온다(멘션이 곧 초대다).
        # 새 스레드를 여는 자리의 참여자는 첫 글이 이미 보여 주므로 들어온 기록을 남기지 않는다
        for pid in [me, *to]:
            if pid != USER:
                _add_member(t, pid, "wrote" if pid == me else "mentioned", by=me, log=bool(replied))
        # 이 스레드에서 처리 중이던 세션이 글을 올리면 처리 중 표시를 내린다. "작업 중"(partial) 답은 아직 하는 중이라 둔다
        if not (a.kind == "reply" and a.status == "partial"):
            t["busy"] = {k: v for k, v in t.get("busy", {}).items() if k.split(".")[0] != me.split(".")[0]}
        _save("threads.json", threads)
        if "." in me and not os.path.exists(_path("cursors", me)):
            _set_cursor(me, os.path.getsize(_path("log.jsonl")) if os.path.exists(_path("log.jsonl")) else 0)
        with open(_path("log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    print(json.dumps({"id": msg["id"], "thread": msg["thread"]}))


def cmd_invite(a):
    """Add a session to a thread. It gets a notice so it can catch up from the start."""
    with _Lock():
        threads = _load("threads.json", {})
        t = threads.get(a.thread)
        if not t:
            sys.exit(f"Thread '{a.thread}' not found.")
        _add_member(t, a.who, "invited", by=_me(a))
        _save("threads.json", threads)
    print(f"Added {a.who} to thread '{t['title']}'. Mention it with an ask or request post saying what you need.")


def _add_member(t, pid, how, by=None, log=True):
    """스레드 참여자로 넣는다. 처음 들어오거나 나갔다 돌아오면 그 시각과 경위(how)를 events에 남긴다.

    패널·웹 페이지가 대화 사이에 "○○ 참여함·나감"을 그리는 기록이다(2026-11-03 사용자 요청). 이미 있는 참여자는 남기지 않는다.
    """
    back = pid in t.get("parted", {})
    t.get("parted", {}).pop(pid, None)
    if pid in t["participants"] and not back:
        return
    if pid not in t["participants"]:
        t["participants"].append(pid)
    if log:
        ev = {"ts": _now(), "who": pid, "what": "joined", "how": how}
        if by and by != pid:
            ev["by"] = by
        t.setdefault("events", []).append(ev)


def _find_thread(threads, key):
    """스레드 ID나 제목(대소문자 무시, 정확히 같은 것)으로 찾는다."""
    if key in threads:
        return key
    hits = [tid for tid, t in threads.items() if t.get("title", "").lower() == key.lower()]
    return hits[0] if len(hits) == 1 else None


def cmd_join(a):
    """Join a thread to take part in it: from now on posts in it may mention you. Prints its recent posts so you can catch up."""
    me = _me(a)
    with _Lock():
        threads = _load("threads.json", {})
        tid = _find_thread(threads, a.thread)
        if not tid:
            sys.exit(f"Thread '{a.thread}' not found. Look with threads --all, or open a new one with post --title.")
        t = threads[tid]
        _add_member(t, me, "joined")
        _save("threads.json", threads)
    recent = [m for m in _log() if m["thread"] == tid][-5:]
    print(f"Joined thread '{t['title']}' ({tid}). Participants: {', '.join(_label(_load('sessions.json', {}), p) for p in t['participants'])}.")
    if recent:
        print("Recent posts:\n")
        _show(_decorate(recent))


def cmd_part(a):
    """Leave one thread when you no longer need it. Posts in it stop reaching you unless someone mentions you again."""
    me = _me(a)
    with _Lock():
        threads = _load("threads.json", {})
        tid = _find_thread(threads, a.thread)
        if not tid:
            sys.exit(f"Thread '{a.thread}' not found.")
        t = threads[tid]
        if me in t["participants"]:
            t["participants"] = [p for p in t["participants"] if p != me]
            t.setdefault("parted", {})[me] = _now()
            t.setdefault("events", []).append({"ts": _now(), "who": me, "what": "left"})
        t["busy"] = {k: v for k, v in t.get("busy", {}).items() if k != me}
        _save("threads.json", threads)
    print(f"Left thread '{t['title']}'.")


def _cursor(sid):
    try:
        with open(os.path.join(ROOT, "cursors", sid), encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return None


def _set_cursor(sid, pos):
    with open(_path("cursors", sid), "w", encoding="utf-8") as f:
        f.write(str(pos))


def _take(sid):
    """Return posts after the cursor that are addressed to me, and move the cursor. The first call starts at the current end."""
    log = _path("log.jsonl")
    size = os.path.getsize(log) if os.path.exists(log) else 0
    pos = _cursor(sid)
    if pos is None:
        _set_cursor(sid, size)
        return []
    mine = []
    # 기록 파일이 줄었거나(정리·손으로 고침) 위치가 줄 중간이면, 죽지 않고 다음 줄 처음부터 읽는다
    if pos > size:
        pos = size
    with open(log, "rb") if os.path.exists(log) else open(os.devnull, "rb") as f:
        if pos > 0:
            f.seek(pos - 1)
            if f.read(1) != b"\n":
                f.readline()
                pos = f.tell()
        f.seek(pos)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            pos += len(raw)
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if m["from"] != sid and sid in m["to"]:
                mine.append(m)
    _set_cursor(sid, pos)
    return mine


ARCHIVE_EVERY = 3600


def _archive(days, force=False):
    """마지막 글이 days일보다 오래된 스레드를 보관 파일(archive/<월>.jsonl)로 옮긴다. 지우지 않는다.

    글 기록은 덧붙이기만 해서 계속 커지고, 패널·세션이 매번 처음부터 읽어 오래 쓰면 느려진다(2026-11-02 사용자 결정).
    읽은 위치(커서)는 파일 안의 바이트 위치라, 남는 줄의 바이트만 세어 새 위치로 옮긴다. 한 시간에 한 번만 돈다.
    잠금 안에서 부른다.
    """
    stamp = _path("archive", ".stamp")
    if not force and os.path.exists(stamp) and time.time() - os.path.getmtime(stamp) < ARCHIVE_EVERY:
        return 0
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(_now())
    log = _path("log.jsonl")
    if days <= 0 or not os.path.exists(log):
        return 0
    with open(log, "rb") as f:
        lines = f.readlines()
    last = {}
    for raw in lines:
        try:
            m = json.loads(raw)
            last[m["thread"]] = max(last.get(m["thread"], ""), m["ts"])
        except (json.JSONDecodeError, KeyError):
            continue
    cutoff = time.time() - days * 86400
    old = set()
    for tid, ts in last.items():
        try:
            if datetime.datetime.fromisoformat(ts).timestamp() < cutoff:
                old.add(tid)
        except ValueError:
            continue
    if not old:
        return 0
    keep, moved, bounds, pos, kept_bytes = [], {}, [], 0, 0
    for raw in lines:
        try:
            m = json.loads(raw) if raw.endswith(b"\n") else None
        except json.JSONDecodeError:
            m = None
        pos += len(raw)
        if m is not None and m.get("thread") in old:
            moved.setdefault(m["ts"][:7], []).append(raw)
        else:
            keep.append(raw)
            kept_bytes += len(raw)
        bounds.append((pos, kept_bytes))
    for month, rows in moved.items():
        with open(_path("archive", f"{month}.jsonl"), "ab") as f:
            f.writelines(rows)
    threads = _load("threads.json", {})
    archived = _load(os.path.join("archive", "threads.json"), {})
    for tid in old:
        if tid in threads:
            archived[tid] = threads.pop(tid)
    _save(os.path.join("archive", "threads.json"), archived)
    _save("threads.json", threads)
    takes = _load("takes.json", {})
    moved_ids = {json.loads(r)["id"] for rows in moved.values() for r in rows}
    _save("takes.json", {k: v for k, v in takes.items() if k not in moved_ids})
    tmp = _path("log.jsonl.tmp")
    with open(tmp, "wb") as f:
        f.writelines(keep)
    # 커서마다 "그 위치 앞에 끝난 줄 중 남은 줄의 바이트 합"이 새 위치다
    cdir = os.path.join(ROOT, "cursors")
    for name in os.listdir(cdir) if os.path.isdir(cdir) else []:
        c = _cursor(name)
        if c is None:
            continue
        new = 0
        for end, kept in bounds:
            if end > c:
                break
            new = kept
        _set_cursor(name, new)
    os.replace(tmp, log)
    return len(old)


def _decorate(msgs):
    sessions = _load("sessions.json", {})
    threads = _load("threads.json", {})
    for m in msgs:
        m["from_title"] = _label(sessions, m["from"])
        m["thread_title"] = threads.get(m["thread"], {}).get("title", "")
        # 세션이 닫혀 있던 사이 쌓인 글은 늦게 받는다. 얼마나 묵은 글인지 같이 알려 아직 유효한지 먼저 보게 한다
        try:
            age = time.time() - datetime.datetime.fromisoformat(m["ts"]).timestamp()
        except ValueError:
            age = 0
        m["age"] = _ago(age) if age > 600 else ""
    return msgs


def _busy_now(t):
    """Sessions currently working on a post in that thread. Drops marks of sessions that left or older than BUSY_MAX."""
    return [s for s, since in t.get("busy", {}).items() if _is_live(s) and time.time() - since < BUSY_MAX]


def cmd_poll(a):
    """For the mod: leave a heartbeat and return new posts that mention this session, as JSON. After leave the list stays empty until profile is called again.

    Marks the thread as being worked on; the mark clears when this session posts there or its turn ends (yield).
    Open user questions need take first, so they get no mark; only the session that took one is marked.
    """
    sid = _me(a)
    if _load("sessions.json", {}).get(sid, {}).get("left"):
        print("[]")
        return
    _beat(sid)
    with _Lock():
        _archive(a.archive_days)
        got = _take(sid)
        for m in got:
            # wake가 없는 옛 글은 받는 세션 모두를 깨우던 때의 글이라 그대로 깨운다
            m["quiet"] = "wake" in m and sid not in m["wake"]
        if got:
            threads = _load("threads.json", {})
            for m in got:
                t = threads.get(m["thread"])
                if t is not None and not m["quiet"] and not m.get("open"):
                    t.setdefault("busy", {})[sid] = time.time()
            _save("threads.json", threads)
        got += _free_notices(sid)
    print(json.dumps(_decorate(got), ensure_ascii=False))


def _free_notices(sid):
    """기다리던 자원이 비었으면(놓음·잡은 세션이 떠남) 알림 글을 만들고 대기 목록에서 뺀다. 잠금 안에서 부른다."""
    waits = _load("waits.json", {})
    mine = [t for t, w in waits.items() if sid in w]
    if not mine:
        return []
    board = _load("board.json", {})
    free = [t for t in mine if t not in board or board[t]["by"] == sid or not _is_live(board[t]["by"])]
    if not free:
        return []
    for t in free:
        waits[t] = [w for w in waits[t] if w != sid]
    _save("waits.json", {t: w for t, w in waits.items() if w})
    text = f"Now free: {', '.join(free)}. Claim before using it (claim {' '.join(free)}); if someone got there first, add --wait again."
    return [{"id": "free-" + uuid.uuid4().hex[:6], "ts": _now(), "from": "chat", "to": [sid], "kind": "notice", "text": text, "thread": "", "wake": [sid], "quiet": False}]


def cmd_take(a):
    """Claim an open user question ("I will answer"). Only one session succeeds per question."""
    sid = _me(a)
    with _Lock():
        takes = _load("takes.json", {})
        cur = takes.get(a.msg)
        if cur and cur != sid and _is_live(cur):
            print(json.dumps({"ok": False, "by": cur, "title": _label(_load("sessions.json", {}), cur)}, ensure_ascii=False))
            return
        msg = next((m for m in _log() if m["id"] == a.msg), None)
        if not msg:
            sys.exit(f"Post '{a.msg}' not found.")
        takes[a.msg] = sid
        _save("takes.json", takes)
        threads = _load("threads.json", {})
        if msg["thread"] in threads:
            threads[msg["thread"]].setdefault("busy", {})[sid] = time.time()
            _save("threads.json", threads)
    print(json.dumps({"ok": True}))


def cmd_yield(a):
    """For the mod: when a turn ends, clear this session's working marks, so a turn that ended without posting does not stay "writing a reply"."""
    sid = _me(a)
    with _Lock():
        threads = _load("threads.json", {})
        n = 0
        for t in threads.values():
            busy = t.get("busy", {})
            mine = [k for k in busy if k.split(".")[0] == sid]
            for k in mine:
                del busy[k]
            n += len(mine)
        if n:
            _save("threads.json", threads)
    print(json.dumps({"released": n}))


def _show(msgs):
    for m in msgs:
        extra = f" · {m['status']}" if m.get("status") else ""
        print(f"[{m['ts']}] {m['from_title']}({m['from']}) · {m['kind']}{extra} · thread '{m['thread_title']}' {m['thread']} · post {m['id']}")
        print(m["text"])
        print()


def cmd_read(a):
    msgs = _decorate(_take(_me(a)))
    _show(msgs) if msgs else print("No new posts")


def cmd_wait(a):
    """Fallback where there is no mod: wait for a post, print it and exit (run it in the background)."""
    sid = _me(a)
    deadline = time.time() + a.timeout if a.timeout else None
    _take(sid)
    while True:
        _beat(sid)
        msgs = _take(sid)
        if msgs:
            _show(_decorate(msgs))
            return
        if deadline and time.time() >= deadline:
            print("Timed out: no new posts")
            return
        time.sleep(POLL)


# 스레드 상태는 "지금 누구 차례인가"로 정한다. 답한 쪽은 대화가 끝났는지 알 수 없으므로, 끝남은 상태가 아니라 시간으로 판정한다
TURN_STATE = {"ask": "waiting", "request": "waiting", "comment": "waiting", "info": "info"}
REPLY_STATE = {"partial": "working", "answered": "answered", "declined": "declined"}


def _thread_states(quiet_hours=6, waiting_hours=24):
    threads = _load("threads.json", {})
    states = {}
    for m in _log():
        if m["thread"] == m["id"]:
            states[m["id"]] = {"root": m, "posts": [], "state": TURN_STATE.get(m["kind"], "waiting"), "updated": m["ts"]}
        elif m["thread"] in states:
            t = states[m["thread"]]
            t["posts"].append(m)
            t["updated"] = m["ts"]
            if m["kind"] == "reply":
                t["state"] = REPLY_STATE.get(m.get("status", "answered"), "answered")
            elif m["kind"] in ("ask", "request") or (m["kind"] == "comment" and m["from"] == USER):
                # 되묻거나 사용자가 의견을 달면 다시 받는 쪽이 답할 차례다
                t["state"] = "waiting"
    for tid, t in states.items():
        meta = threads.get(tid, {})
        t["id"] = tid
        t["title"] = meta.get("title") or t["root"]["text"][:30]
        t["participants"] = meta.get("participants", [])
        t["parted"] = meta.get("parted", {})
        t["events"] = meta.get("events", [])
        # 지금 글을 받아 처리 중인 세션들. 패널이 그 세션 옆에 "답을 쓰는 중"을 띄운다
        t["busy"] = _busy_now(meta)
        # 답 기다림: 멘션을 받고 그 뒤로 이 스레드에 아직 아무 글도 안 올린 세션. 멘션 없는 사용자 질문은 손 든 세션이 답할 때까지 열려 있다
        takes = _load("takes.json", {})
        pending, open_q = {}, None
        for m in [t["root"], *t["posts"]]:
            pending.pop(m["from"], None)
            if m.get("open"):
                open_q = {"id": m["id"], "ts": m["ts"], "taker": takes.get(m["id"], "")}
            elif open_q and m["from"] == open_q["taker"]:
                open_q = None
            if m["kind"] in ("ask", "request", "comment") and not m.get("open"):
                for who in m["to"]:
                    pending[who] = m["ts"]
        t["pending"] = [{"who": w, "since": s} for w, s in pending.items() if w != USER]
        if open_q:
            age = time.time() - datetime.datetime.fromisoformat(open_q["ts"]).timestamp()
            t["open"] = {"taker": open_q["taker"], "nobody": not open_q["taker"] and age > OPEN_WAIT}
        else:
            t["open"] = None
        if t["pending"] or t["open"]:
            t["state"] = "waiting"
        try:
            quiet = time.time() - datetime.datetime.fromisoformat(t["updated"]).timestamp()
        except ValueError:
            quiet = 0
        # 답을 못 받은 스레드는 사용자가 알아야 하므로 더 오래 열어 둔다
        limit = (waiting_hours if t["state"] == "waiting" else quiet_hours) * 3600
        t["closed"] = quiet > limit
        t["quiet"] = _ago(quiet) if quiet > 3600 else ""
    return states


def cmd_threads(a):
    me = _me(a)
    sessions = _load("sessions.json", {})
    shown = 0
    for tid, t in sorted(_thread_states().items(), key=lambda x: x[1]["updated"], reverse=True):
        if not a.all and t["closed"]:
            continue
        if not a.all and me not in t["participants"]:
            continue
        who = ", ".join(_label(sessions, p) for p in t["participants"])
        print(f"{tid} · {t['state']} · '{t['title']}' · {who}")
        shown += 1
    if not shown:
        print("No open threads")


def cmd_claim(a):
    """Claim all targets at once. If any is claimed by someone else, claim none.

    Claiming one by one can stall halfway, with two sessions each waiting for what the other holds.
    """
    me = _me(a)
    targets = [os.path.abspath(t) if os.path.exists(t) or os.sep in t else t for t in a.targets]
    with _Lock():
        board = _load("board.json", {})
        sessions = _load("sessions.json", {})
        taken = [(t, board[t]) for t in targets if t in board and board[t]["by"] != me and _is_live(board[t]["by"])]
        if taken and not a.force:
            who = ", ".join(f"'{t}' by {_label(sessions, c['by'])}({c['by']})" for t, c in taken)
            if a.wait:
                # 기다리는 세션을 적어 둔다. 잡은 세션이 놓거나 떠나면 poll이 그 세션에 알림 한 번으로 턴을 연다.
                # 세션마다 "잡음·놓음"을 글로 써서 서로를 멘션하던 것을 대신한다(2026-11-03 사용자 결정)
                waits = _load("waits.json", {})
                for t, _ in taken:
                    if me not in waits.setdefault(t, []):
                        waits[t].append(me)
                _save("waits.json", waits)
                sys.exit(f"Claimed nothing: already claimed: {who}. You are on the wait list; you will get a notice when it is free. Do other work or end the turn meanwhile.")
            sys.exit(f"Claimed nothing: already claimed: {who}. Ask that session, or add --wait to get a notice when it is free.")
        for t in targets:
            board[t] = {"by": me, "since": _now(), "note": a.note or ""}
        _save("board.json", board)
        waits = _load("waits.json", {})
        if any(me in waits.get(t, []) for t in targets):
            for t in targets:
                waits[t] = [w for w in waits.get(t, []) if w != me]
            _save("waits.json", {t: w for t, w in waits.items() if w})
    print(f"Claimed {len(targets)}: " + ", ".join(targets))


def cmd_release(a):
    me = _me(a)
    with _Lock():
        board = _load("board.json", {})
        # --all은 옵션으로 받는다. 위치 인자 하나로 받던 때는 argparse가 --all을 모르는 옵션으로 보고 실패했다
        wanted = {os.path.abspath(t) for t in a.targets} | set(a.targets)
        gone = [t for t, c in board.items() if c["by"] == me and (a.all or t in wanted)]
        for t in gone:
            del board[t]
        _save("board.json", board)
    print(f"Released {len(gone)}.")


def cmd_leave(a):
    """Quit the whole chat: remove the heartbeat and release claims. Nothing reaches this session until the user sends it a prompt again.

    A session that quit cannot be called by any thread or session, so this needs --user-asked: only for when the user said so in this session's own window.
    """
    me = _me(a)
    if not a.user_asked and "." not in me:
        sys.exit("Not left. Quitting the session chat cuts this session off from every thread and session. To leave one thread, run part <thread id>. Run leave --user-asked only if the user told you in this window to quit the session chat.")
    try:
        os.remove(os.path.join(ROOT, "alive", me))
    except FileNotFoundError:
        pass
    with _Lock():
        board = _load("board.json", {})
        for t in [t for t, c in board.items() if c["by"].split(".")[0] == me]:
            del board[t]
        _save("board.json", board)
        waits = _load("waits.json", {})
        _save("waits.json", {t: [x for x in w if x.split(".")[0] != me] for t, w in waits.items() if any(x.split(".")[0] != me for x in w)})
        sessions = _load("sessions.json", {})
        if me in sessions:
            sessions[me]["left"] = _now()
            _save("sessions.json", sessions)
    print("Left the session chat.")


def cmd_dump(a):
    """For the panel: live sessions, threads, claims and overlaps in one object."""
    print(json.dumps(_dump_data(a.quiet_hours, a.waiting_hours, a.limit), ensure_ascii=False))


def _dump_data(quiet_hours, waiting_hours, limit):
    live = _live_sessions()
    for sid, p in live.items():
        av = p.get("avatar", {"shape": "terminal", "color": "slate"})
        p["avatar"] = {**av, "hex": COLORS.get(av.get("color"), COLORS["slate"])}
        p["overlaps"] = [o["id"] for o in _overlaps(sid, _load("sessions.json", {}))]
        idle = _idle(p)
        p["idle"] = _ago(idle) if idle > IDLE else ""
        p.pop("recent", None)
    threads = sorted(_thread_states(quiet_hours, waiting_hours).values(), key=lambda t: t["updated"], reverse=True)[:limit]
    board = [{"target": t, **c} for t, c in sorted(_load("board.json", {}).items()) if _is_live(c["by"])]
    # 스레드에 나오는 떠난 세션의 이름·아바타. 살아 있는 목록에는 없으므로 따로 싣는다
    names = {}
    for sid, p in _load("sessions.json", {}).items():
        av = p.get("avatar", {"shape": "terminal", "color": "slate"})
        # seen: 마지막으로 쓰인 시각. 패널이 꺼진 세션까지 초대 목록에 보일 때 최근 것부터 놓는다. left: 대화방을 나간 세션은 초대 목록에서 뺀다
        seen = max(p.get("last_active", 0), datetime.datetime.fromisoformat(p["updated"]).timestamp() if p.get("updated") else 0)
        names[sid] = {"title": p.get("title", ""), "avatar": {**av, "hex": COLORS.get(av.get("color"), COLORS["slate"])}, "seen": seen, "left": bool(p.get("left"))}
    return {"sessions": live, "names": names, "threads": threads, "board": board}


# 웹 페이지 서버를 하나만 두기 위한 기록(web.json): 번호(port)와 지금 도는 서버의 표식(token).
# 번호는 처음에 운영체제가 고른 빈 번호이고, 서버가 내려가도 남겨 두어 다음에 같은 주소로 다시 띄운다
WEB_IDLE = 300


def _web_alive(rec):
    """기록의 서버가 살아 있고 우리 서버(표식이 같은)인지 본다. 같은 번호를 다른 프로그램이 잡았으면 False."""
    import urllib.request

    if not rec.get("port") or not rec.get("token"):
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{rec['port']}/state.json?probe=1", timeout=1) as r:
            return json.load(r).get("token") == rec["token"]
    except Exception:
        return False


def cmd_serve(a):
    """Read-only web page of the chat at http://127.0.0.1:<port>/. Listens on this computer only and accepts no writes.

    Comments stay in the panel: they are signed with a key only the mod holds, so a page that could post would let anything on this computer speak as the user.
    With --detach (what the mod uses) it starts one server apart from the calling session, or reuses the one already running, prints its address and returns.
    The server stops by itself when no page has asked for the state for WEB_IDLE seconds.
    """
    if a.detach:
        return _serve_detached(a)
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")
    token = a.token or uuid.uuid4().hex
    last_seen = [time.time()]

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, kind):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/state.json":
                # 자기 확인용 요청(probe)은 세지 않는다. 페이지가 묻는 것만 "보고 있다"로 센다
                if "probe=1" not in self.path:
                    last_seen[0] = time.time()
                state = _dump_data(a.quiet_hours, a.waiting_hours, a.limit)
                # 언어는 기록(web.json)에서 매번 읽는다. 버튼을 누를 때 그때의 언어를 적어 두므로, 설정을 바꾸면 서버를 다시 띄우지 않아도 바로 반영된다
                state["lang"] = _load("web.json", {}).get("lang") or LANG
                state["token"] = token
                self._send(200, json.dumps(state, ensure_ascii=False), "application/json; charset=utf-8")
            elif path in ("/", "/index.html"):
                with open(page, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif re.fullmatch(r"/avatars/[a-z]+\.svg", path) and os.path.exists(os.path.join(os.path.dirname(page), "..", path.lstrip("/"))):
                with open(os.path.join(os.path.dirname(page), "..", path.lstrip("/")), encoding="utf-8") as f:
                    self._send(200, f.read(), "image/svg+xml; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")

        def log_message(self, format, *args):
            pass

    # 정한 번호를 먼저 잡아 보고, 막혀 있으면 운영체제가 고른 빈 번호로 띄운다
    server = None
    for port in ([a.port] if a.port else []) + [0]:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    assert server is not None
    port = server.server_address[1]
    idle = a.idle or WEB_IDLE
    _save("web.json", {"port": port, "token": token, "pid": os.getpid(), "lang": LANG})

    def stop_when_idle():
        while True:
            time.sleep(min(60, idle))
            if time.time() - last_seen[0] > idle:
                # 번호는 남기고 표식만 지운다. 다음에 같은 주소로 다시 띄우기 위해서다
                rec = _load("web.json", {})
                if rec.get("token") == token:
                    _save("web.json", {"port": port})
                server.shutdown()
                return

    threading.Thread(target=stop_when_idle, daemon=True).start()
    print(f"http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


def _serve_detached(a):
    rec = _load("web.json", {})
    if _web_alive(rec):
        if rec.get("lang") != LANG:
            with _Lock():
                rec = _load("web.json", {})
                rec["lang"] = LANG
                _save("web.json", rec)
        print(f"http://127.0.0.1:{rec['port']}/")
        return
    token = uuid.uuid4().hex
    args = [sys.executable, os.path.abspath(__file__), "--lang", LANG, "serve", "--token", token, "--port", str(rec.get("port") or 0),
            "--quiet-hours", str(a.quiet_hours), "--waiting-hours", str(a.waiting_hours), "--limit", str(a.limit)]
    # 부른 세션과 떨어진 프로세스로 띄운다. 모드 문서대로 "명령이 놓아 준 프로세스"는 세션이 끝나도 남는다
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    # 서버가 번호를 잡고 기록을 쓸 때까지 기다린다
    for _ in range(50):
        time.sleep(0.1)
        rec = _load("web.json", {})
        if rec.get("token") == token:
            print(f"http://127.0.0.1:{rec['port']}/")
            return
    sys.exit("The web page server did not start.")


def cmd_paths(a):
    print(json.dumps({"root": ROOT}))


def cmd_whoami(a):
    print(_me(a))


def main():
    ap = argparse.ArgumentParser(description="Claude Code sessions on the same machine ask and answer each other in threads")
    ap.add_argument("--id", help="Participant ID. Defaults to the first 8 characters of CLAUDE_CODE_SESSION_ID")
    ap.add_argument("--sub", help="Subagent label. The ID becomes <session ID>.<label>")
    ap.add_argument("--lang", choices=("en", "ko"), default="en", help="Language of durations in the output (the panel passes the user's language)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("profile", help="Set name and current work (returns sessions that may overlap)")
    s.add_argument("--title", help="A name that tells the user what this session works on")
    s.add_argument("--doing", help="One line on the current work")
    s.add_argument("--cwd")
    s.add_argument("--host")
    s.add_argument("--parent", help="ID of the session that started this one (given in its instructions)")
    s.add_argument("--group", help="Session group name in the desktop app sidebar. The panel groups by it")
    s.add_argument("--app-title", action="store_true", help="--title is the app's own session title; automatic summaries keep it")
    s.add_argument("--auto", action="store_true", help="Automatic summary from the mod; does not overwrite values the session set recently")
    s.add_argument("--avatar", choices=SHAPES)
    s.add_argument("--color", choices=list(COLORS))
    s = sub.add_parser("who", help="List live sessions")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("find", help="Find sessions to ask, with reasons")
    s.add_argument("query", help="A file path or words about the work")
    s = sub.add_parser("post", help="Send a post. Mention recipients with --to")
    s.add_argument("--to", help="Session IDs to mention (comma separated). Empty in a reply mentions the author being answered")
    s.add_argument("--open", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--top", action="store_true", help="A new post in the thread rather than a reply (the panel's bottom box)")
    s.add_argument("--kind", choices=KINDS, help="Defaults to reply with --reply-to, ask for a new thread")
    s.add_argument("--title", help="Title of a new thread (shown to the user)")
    s.add_argument("--reply-to", help="ID of the post to reply to")
    s.add_argument("--status", choices=("answered", "declined", "partial"), help="Required for reply: answered (done), partial (in progress), declined (will not do)")
    s.add_argument("--sig", help=argparse.SUPPRESS)
    s.add_argument("text")
    s = sub.add_parser("invite", help="Add a session to a thread")
    s.add_argument("thread")
    s.add_argument("who")
    s = sub.add_parser("join", help="Join a thread (by ID or exact title) and see its recent posts")
    s.add_argument("thread")
    s = sub.add_parser("part", help="Leave one thread when you no longer need it")
    s.add_argument("thread")
    sub.add_parser("read", help="Read new posts")
    s = sub.add_parser("wait", help="Without the mod: wait until a post arrives (run in the background)")
    s.add_argument("--timeout", type=int, default=0)
    s = sub.add_parser("threads", help="My open threads")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("claim", help="Claim work. Give all targets at once; if any is taken, none is claimed")
    s.add_argument("targets", nargs="+")
    s.add_argument("--note")
    s.add_argument("--force", action="store_true", help="Take over a claim held by another live session (only when the user decided so)")
    s.add_argument("--wait", action="store_true", help="If taken, join the wait list and get a notice when it is free")
    s = sub.add_parser("release", help="Release claims (--all for every one)")
    s.add_argument("targets", nargs="*")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("leave", help="Quit the whole session chat (to leave one thread, use part)")
    s.add_argument("--user-asked", action="store_true", help="Required: the user told you in this session's own window to quit the session chat")
    s = sub.add_parser("touch", help="For the mod: record an edited file as recent")
    s.add_argument("path")
    s = sub.add_parser("check", help="For the mod: check for overlaps before editing a file")
    s.add_argument("path")
    s = sub.add_parser("poll", help="For the mod: heartbeat and new posts")
    s.add_argument("--archive-days", type=float, default=7, help="Move threads whose last post is older than this many days to archive/ (0 keeps everything)")
    s = sub.add_parser("activity", help="For the mod: record that a user prompt or turn happened")
    s.add_argument("--user", action="store_true", help="A user prompt: also rejoins a session that had quit the chat")
    sub.add_parser("yield", help="For the mod: clear working marks when a turn ends")
    s = sub.add_parser("take", help="Claim an open user question. Only one session succeeds per question")
    s.add_argument("msg", help="ID of that question post")
    s = sub.add_parser("dump", help="State for the panel")
    s.add_argument("--quiet-hours", type=float, default=6, help="Hours of quiet after the last post before a thread counts as ended")
    s.add_argument("--waiting-hours", type=float, default=24, help="Hours before a thread still waiting for a reply counts as ended")
    s.add_argument("--limit", type=int, default=30)
    s = sub.add_parser("serve", help="Read-only web page of the chat on 127.0.0.1")
    s.add_argument("--detach", action="store_true", help="Start one server apart from this session (or reuse the running one), print its address and return")
    s.add_argument("--port", type=int, default=0, help="Port to try first; 0 lets the system choose")
    s.add_argument("--token", help=argparse.SUPPRESS)
    s.add_argument("--idle", type=float, default=0, help=argparse.SUPPRESS)
    s.add_argument("--quiet-hours", type=float, default=6)
    s.add_argument("--waiting-hours", type=float, default=24)
    s.add_argument("--limit", type=int, default=30)
    sub.add_parser("paths", help="Storage location")
    sub.add_parser("whoami", help="This session's ID")
    a = ap.parse_args()
    global LANG
    LANG = a.lang
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
