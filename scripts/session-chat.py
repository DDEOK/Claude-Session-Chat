#!/usr/bin/env python3
"""같은 기기의 Claude Code 세션끼리 스레드로 정보·작업을 요청하고 답하는 창구.

**전달은 모드가 한다.** 플러그인의 모드가 세션마다 몇 초에 한 번 `poll`을 불러 자기 앞으로 온 글을 받아, 세션이 쉬고 있을 때 그 글로 새 턴을 연다. 모드가 없는 환경(다른 에이전트 등)은 `wait`를 백그라운드로 켜 두는 대체 수단을 쓴다.

**누구인가.** 세션끼리는 고유 ID로 주고받는다. 참여자 ID는 환경 변수 `CLAUDE_CODE_SESSION_ID`의 앞 8자이고(모드는 `--id`로 준다), 서브에이전트는 `--sub <라벨>`로 `<ID>.<라벨>`을 쓴다. 사람이 보는 것은 세션이 스스로 지은 이름과 스레드 이름이다.

**자리**: `~/.cache/session-chat/` 아래 한 곳. 이 기기의 로컬 상태이고 어느 저장소에도 두지 않는다.

- `sessions.json`: 세션 프로필. 이름·하는 일(세션이 씀), 폴더·저장소·브랜치·최근 파일(모드가 씀), 아바타.
- `alive/<ID>`: 생존 신호. 모드가 몇 초마다 갱신하고, 끊긴 세션은 목록에서 빠진다.
- `log.jsonl`: 모든 글의 시간순 원본. 덧붙이기만 한다.
- `threads.json`: 스레드 이름·연 세션·참여자. 상태는 로그의 마지막 답에서 계산한다.
- `board.json`: 지금 누가 무엇을 맡았나.
- `cursors/<ID>`: 그 참여자가 어디까지 읽었나.

세션이 지킬 규칙은 `skills/session-chat/SKILL.md`가 원본이다. macOS·Linux·Windows에서 돈다(잠금은 `fcntl`, Windows는 `msvcrt`). 파일은 모두 UTF-8로 읽고 쓴다.
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
        sys.exit("세션 ID를 알 수 없다. CLAUDE_CODE_SESSION_ID가 없는 환경이면 --id로 준다.")
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
        return "사용자"
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


def _ago(seconds):
    m = int(seconds // 60)
    if m < 60:
        return f"{m}분"
    h, m = divmod(m, 60)
    return f"{h}시간 {m}분" if m and h < 24 else (f"{h}시간" if h < 24 else f"{h // 24}일")


def cmd_activity(a):
    """모드가 사람 요청이나 턴이 있을 때 부른다. 생존 신호와 달리 세션이 실제로 쓰이고 있다는 표시다."""
    sid = _me(a)
    with _Lock():
        sessions = _load("sessions.json", {})
        if sid in sessions:
            sessions[sid]["last_active"] = time.time()
            _save("sessions.json", sessions)


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
    """요청할 세션 후보를 이유와 함께 고른다. 맡은 것 → 최근 파일 → 같은 저장소 → 하는 일 글자 순으로 점수를 준다."""
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
                why.append(f"맡음: {target}")
        for r in p.get("recent", []):
            if r["path"] == qpath or (len(q) > 2 and q.lower() in r["path"].lower()):
                score += 50
                why.append(f"최근 고침: {r['path']}")
                break
        if qpath and p.get("repo") and qpath.startswith(p["repo"] + os.sep):
            score += 20
            why.append(f"같은 저장소: {p['repo']}")
        hit = words & (_words(p.get("doing")) | _words(p.get("title")))
        if hit:
            score += 10 * len(hit)
            why.append("하는 일: " + ", ".join(sorted(hit)))
        idle = _idle(p)
        if score and idle > IDLE:
            # 오래 쉰 세션은 답이 늦고, 받는 턴이 캐시가 만료된 대화를 다시 처리해 비싸다
            score = max(1, score - 30)
            why.append(f"{_ago(idle)}째 쉬는 중이라 답이 늦을 수 있다")
        if score:
            out.append({"id": sid, "title": p.get("title", ""), "doing": p.get("doing", ""), "score": score, "why": why})
    out.sort(key=lambda x: -x["score"])
    print(json.dumps(out[:5], ensure_ascii=False, indent=1) if out else "후보 없음: who로 전체 목록을 보거나 사용자에게 누구에게 물을지 묻는다.")


def cmd_who(a):
    sessions = _live_sessions()
    if a.json:
        print(json.dumps(sessions, ensure_ascii=False))
        return
    if not sessions:
        print("살아 있는 세션 없음")
    for sid, p in sorted(sessions.items()):
        idle = _idle(p)
        rest = f" · {_ago(idle)}째 쉬는 중" if idle > IDLE else ""
        print(f"{sid} · {p.get('title') or '(이름 없음)'} · {p.get('doing', '')} · {p.get('repo') or p.get('cwd', '')}{rest}")


def _resolve(to, sessions):
    if to == USER or to in sessions or to.split(".")[0] in sessions:
        return to
    hits = [sid for sid, p in sessions.items() if p.get("title") == to]
    if len(hits) == 1:
        return hits[0]
    sys.exit(f"'{to}'은 살아 있는 세션이 아니다. who나 find로 ID를 찾는다.")


def cmd_post(a):
    """글을 올린다. 받는 사람은 멘션(--to)이다. 멘션된 세션만 그 글을 받는다.

    - 답(reply·comment)에서 --to를 비우면 답하는 글을 쓴 쪽을 자동으로 멘션한다. 멘션을 빠뜨려 물은 쪽이 답을 못 받는 일을 막는다.
    - 멘션 없이 묻는 것은 사용자만 할 수 있다(--open). 그 글은 스레드의 살아 있는 세션 모두에게 가고, take로 먼저 손 든 세션만 답한다.
    """
    me = _me(a)
    sessions = _live_sessions()
    log = _log()
    replied = next((m for m in log if m["id"] == a.reply_to), None) if a.reply_to else None
    if a.reply_to and not replied:
        sys.exit(f"글 '{a.reply_to}'을 찾지 못했다.")
    threads = _load("threads.json", {})
    t_meta = threads.get(replied["thread"], {}) if replied else {}
    if a.open:
        if me != USER:
            sys.exit("멘션 없이 묻기(--open)는 사용자만 쓴다. 세션은 --to로 받을 세션을 멘션한다.")
        names = [p for p in t_meta.get("participants", []) if p.split(".")[0] in sessions]
    else:
        names = [t for t in (a.to or "").split(",") if t]
        if not names and replied:
            names = [replied["from"]]
    # 사용자 글은 지금 살아 있는 세션에게만 간다. 떠난 참여자 하나 때문에 글 전체가 막히지 않게 한다
    if me == USER:
        names = [t for t in names if t.split(".")[0] in sessions]
    to = [_resolve(t, sessions) for t in names if t != me]
    if not to:
        sys.exit("받을 세션이 없다. --to로 멘션할 세션 ID를 준다(who·find로 찾는다).")
    strangers = [t for t in to if t_meta and t != USER and t not in t_meta.get("participants", [])]
    if strangers:
        print(f"주의: {', '.join(_label(sessions, s) for s in strangers)}은(는) 이 스레드에 없던 세션이다. 멘션이 맞는지 확인한다.", file=sys.stderr)
    # 종류를 안 적으면 받은 글에 이어 쓰는 것은 답(reply)으로 본다. 기본값이 질문이던 때 답이 "질문"으로 올라가 상대가 또 답할 차례로 잡혔다
    if not a.kind:
        a.kind = "comment" if me == USER and replied else ("reply" if replied else "ask")
    msg = {"id": uuid.uuid4().hex[:8], "ts": _now(), "from": me, "to": to, "kind": a.kind, "text": a.text}
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
                    sys.exit(f"이 질문은 {_label(sessions, taker)}이(가) 이미 답하고 있다. 아무것도 올리지 않는다.")
                takes[replied["id"]] = me
                _save("takes.json", takes)
            if a.kind == "reply":
                # 기본값을 두지 않는다. 기본이 "끝남"이면 답 하나로 진행 중인 스레드가 끝난 것으로 보였다
                if not a.status:
                    sys.exit("reply에는 --status가 필요하다: 요청을 다 마쳤으면 answered, 아직 하는 중이면 partial, 안 하면 declined.")
                msg["status"] = a.status
            t = threads.setdefault(msg["thread"], {"title": "", "by": replied["from"], "participants": [replied["from"], *replied["to"]], "created": replied["ts"]})
        else:
            if a.kind in ("reply", "comment"):
                sys.exit(f"{a.kind}에는 --reply-to <글 id>가 필요하다.")
            if not a.title:
                sys.exit("새 스레드에는 --title이 필요하다. 사용자가 보고 무슨 일인지 알 수 있는 이름을 준다.")
            msg["thread"] = msg["id"]
            t = threads[msg["id"]] = {"title": a.title, "by": me, "participants": [], "created": msg["ts"]}
        for pid in [me, *to]:
            if pid != USER and pid not in t["participants"]:
                t["participants"].append(pid)
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
    """스레드에 세션을 더 끼운다. 그 세션에는 스레드를 처음부터 요약할 수 있게 알림 글이 간다."""
    with _Lock():
        threads = _load("threads.json", {})
        t = threads.get(a.thread)
        if not t:
            sys.exit(f"스레드 '{a.thread}'를 찾지 못했다.")
        if a.who not in t["participants"]:
            t["participants"].append(a.who)
        _save("threads.json", threads)
    print(f"'{t['title']}' 스레드에 {a.who}를 끼웠다. 그 세션에 info로 무엇을 원하는지 보낸다.")


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
    """커서 뒤의 글 중 내 앞으로 온 것을 돌려주고 커서를 옮긴다. 처음이면 지금 끝에서 시작한다."""
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
    """그 스레드에서 지금 글을 받아 처리 중인 세션들. 떠났거나 BUSY_MAX가 지난 표시는 뺀다."""
    return [s for s, since in t.get("busy", {}).items() if _is_live(s) and time.time() - since < BUSY_MAX]


def cmd_poll(a):
    """모드용: 생존 신호를 남기고, 이 세션을 멘션한 새 글을 JSON으로 받는다. leave한 세션은 다시 profile을 부르기 전까지 빈 목록이다.

    받은 스레드에는 "처리 중" 표시를 남긴다. 그 스레드에 글을 올리거나 턴이 끝나면(yield) 내린다.
    멘션 없는 사용자 질문(open)은 손들기(take)가 필요하므로 처리 중 표시를 남기지 않는다. 손 든 세션만 표시된다.
    """
    sid = _me(a)
    if _load("sessions.json", {}).get(sid, {}).get("left"):
        print("[]")
        return
    _beat(sid)
    with _Lock():
        got = _take(sid)
        if got:
            threads = _load("threads.json", {})
            for m in got:
                t = threads.get(m["thread"])
                if t is not None and m["kind"] != "info" and not m.get("open"):
                    t.setdefault("busy", {})[sid] = time.time()
            _save("threads.json", threads)
    print(json.dumps(_decorate(got), ensure_ascii=False))


def cmd_take(a):
    """멘션 없는 사용자 질문에 "내가 답한다"고 손을 든다. 한 질문에 한 세션만 성공한다."""
    sid = _me(a)
    with _Lock():
        takes = _load("takes.json", {})
        cur = takes.get(a.msg)
        if cur and cur != sid and _is_live(cur):
            print(json.dumps({"ok": False, "by": cur, "title": _label(_load("sessions.json", {}), cur)}, ensure_ascii=False))
            return
        msg = next((m for m in _log() if m["id"] == a.msg), None)
        if not msg:
            sys.exit(f"글 '{a.msg}'을 찾지 못했다.")
        takes[a.msg] = sid
        _save("takes.json", takes)
        threads = _load("threads.json", {})
        if msg["thread"] in threads:
            threads[msg["thread"]].setdefault("busy", {})[sid] = time.time()
            _save("threads.json", threads)
    print(json.dumps({"ok": True}))


def cmd_yield(a):
    """모드용: 턴이 끝나면 이 세션의 처리 중 표시를 모두 내린다. 답을 올리지 않고 끝낸 턴이 "답을 쓰는 중"으로 남지 않게 한다."""
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
        print(f"[{m['ts']}] {m['from_title']}({m['from']}) · {m['kind']}{extra} · 스레드 '{m['thread_title']}' {m['thread']} · 글 {m['id']}")
        print(m["text"])
        print()


def cmd_read(a):
    msgs = _decorate(_take(_me(a)))
    _show(msgs) if msgs else print("새 글 없음")


def cmd_wait(a):
    """모드가 없는 환경의 대체 수단: 글이 올 때까지 기다렸다가 출력하고 끝난다(백그라운드로 켠다)."""
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
            print("시간 초과: 새 글 없음")
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
        print("열린 스레드 없음")


def cmd_claim(a):
    """맡을 대상을 한 번에 전부 잡는다. 하나라도 남이 맡았으면 하나도 잡지 않는다.

    하나씩 잡으면 반만 잡은 채 막혀, 두 세션이 서로 상대가 잡은 것을 기다리는 교착이 생길 수 있다.
    """
    me = _me(a)
    targets = [os.path.abspath(t) if os.path.exists(t) or os.sep in t else t for t in a.targets]
    with _Lock():
        board = _load("board.json", {})
        sessions = _load("sessions.json", {})
        taken = [(t, board[t]) for t in targets if t in board and board[t]["by"] != me and _is_live(board[t]["by"])]
        if taken and not a.force:
            who = ", ".join(f"'{t}'은 {_label(sessions, c['by'])}({c['by']})" for t, c in taken)
            sys.exit(f"하나도 맡지 않았다: {who}가 맡고 있다. 그 세션에 묻는다.")
        for t in targets:
            board[t] = {"by": me, "since": _now(), "note": a.note or ""}
        _save("board.json", board)
    print(f"{len(targets)}건을 맡았다: " + ", ".join(targets))


def cmd_release(a):
    me = _me(a)
    with _Lock():
        board = _load("board.json", {})
        gone = [t for t, c in board.items() if c["by"] == me and (a.target in ("--all", t) or os.path.abspath(a.target) == t)]
        for t in gone:
            del board[t]
        _save("board.json", board)
    print(f"{len(gone)}건을 풀었다.")


def cmd_leave(a):
    """대화방에서 빠진다: 생존 신호를 지우고 맡은 것을 푼다. 모드는 사용자가 다시 켜기 전까지 전달을 멈춘다."""
    me = _me(a)
    try:
        os.remove(os.path.join(ROOT, "alive", me))
    except FileNotFoundError:
        pass
    with _Lock():
        board = _load("board.json", {})
        for t in [t for t, c in board.items() if c["by"].split(".")[0] == me]:
            del board[t]
        _save("board.json", board)
        sessions = _load("sessions.json", {})
        if me in sessions:
            sessions[me]["left"] = _now()
            _save("sessions.json", sessions)
    print("대화방에서 빠졌다.")


def cmd_dump(a):
    """패널용: 살아 있는 세션·스레드·맡은 것·겹침을 한 덩어리로."""
    live = _live_sessions()
    for sid, p in live.items():
        av = p.get("avatar", {"shape": "terminal", "color": "slate"})
        p["avatar"] = {**av, "hex": COLORS.get(av.get("color"), COLORS["slate"])}
        p["overlaps"] = [o["id"] for o in _overlaps(sid, _load("sessions.json", {}))]
        idle = _idle(p)
        p["idle"] = _ago(idle) if idle > IDLE else ""
        p.pop("recent", None)
    threads = sorted(_thread_states(a.quiet_hours, a.waiting_hours).values(), key=lambda t: t["updated"], reverse=True)[: a.limit]
    board = [{"target": t, **c} for t, c in sorted(_load("board.json", {}).items()) if _is_live(c["by"])]
    # 스레드에 나오는 떠난 세션의 이름·아바타. 살아 있는 목록에는 없으므로 따로 싣는다
    names = {}
    for sid, p in _load("sessions.json", {}).items():
        av = p.get("avatar", {"shape": "terminal", "color": "slate"})
        names[sid] = {"title": p.get("title", ""), "avatar": {**av, "hex": COLORS.get(av.get("color"), COLORS["slate"])}}
    print(json.dumps({"sessions": live, "names": names, "threads": threads, "board": board}, ensure_ascii=False))


def cmd_paths(a):
    print(json.dumps({"root": ROOT}))


def cmd_whoami(a):
    print(_me(a))


def main():
    ap = argparse.ArgumentParser(description="같은 기기의 Claude Code 세션끼리 스레드로 요청하고 답한다")
    ap.add_argument("--id", help="참여자 ID. 생략하면 CLAUDE_CODE_SESSION_ID 앞 8자")
    ap.add_argument("--sub", help="서브에이전트 라벨. ID가 <세션 ID>.<라벨>이 된다")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("profile", help="이름·하는 일을 적는다(겹칠 수 있는 세션을 돌려준다)")
    s.add_argument("--title", help="사용자가 보고 무슨 일을 하는 세션인지 알 수 있는 이름")
    s.add_argument("--doing", help="지금 하는 일 한 줄")
    s.add_argument("--cwd")
    s.add_argument("--host")
    s.add_argument("--parent", help="이 세션을 띄운 세션의 ID(띄운 세션이 지시문에 적어 준다)")
    s.add_argument("--group", help="앱의 세션 그룹 이름(데스크톱 앱 사이드바). 패널이 이 이름으로 묶는다")
    s.add_argument("--app-title", action="store_true", help="--title이 앱이 붙인 세션 제목 그대로라는 표시. 자동 요약이 이름을 바꾸지 않는다")
    s.add_argument("--auto", action="store_true", help="모드의 자동 요약. 세션이 최근 직접 고친 값은 덮어쓰지 않는다")
    s.add_argument("--avatar", choices=SHAPES)
    s.add_argument("--color", choices=list(COLORS))
    s = sub.add_parser("who", help="살아 있는 세션 목록")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("find", help="요청할 세션 후보를 이유와 함께 찾는다")
    s.add_argument("query", help="파일 경로나 하는 일 낱말")
    s = sub.add_parser("post", help="글을 보낸다. 받는 세션을 --to로 멘션한다")
    s.add_argument("--to", help="멘션할 세션 ID(쉼표로 여럿). 답할 때 비우면 답하는 글을 쓴 쪽을 멘션한다")
    s.add_argument("--open", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--top", action="store_true", help="답글이 아니라 스레드에 새로 쓰는 글(패널의 아래 입력창)")
    s.add_argument("--kind", choices=KINDS, help="생략하면 받은 글에 이어 쓸 때(--reply-to)는 reply, 새 스레드는 ask")
    s.add_argument("--title", help="새 스레드의 이름(사용자가 보는 것)")
    s.add_argument("--reply-to", help="이어 쓸 글 id")
    s.add_argument("--status", choices=("answered", "declined", "partial"), help="reply에 꼭 준다: answered(요청을 다 마침) · partial(진행 중) · declined(안 함)")
    s.add_argument("--sig", help=argparse.SUPPRESS)
    s.add_argument("text")
    s = sub.add_parser("invite", help="스레드에 세션을 끼운다")
    s.add_argument("thread")
    s.add_argument("who")
    sub.add_parser("read", help="새 글을 읽는다")
    s = sub.add_parser("wait", help="모드가 없을 때: 글이 올 때까지 기다린다(백그라운드로 켠다)")
    s.add_argument("--timeout", type=int, default=0)
    s = sub.add_parser("threads", help="내 열린 스레드")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("claim", help="맡은 일을 올린다. 여럿이면 한 번에 다 주고, 하나라도 남이 맡았으면 하나도 잡지 않는다")
    s.add_argument("targets", nargs="+")
    s.add_argument("--note")
    s.add_argument("--force", action="store_true", help="살아 있는 다른 세션이 맡은 것도 가져온다(사용자가 정했을 때만)")
    s = sub.add_parser("release", help="맡은 것을 푼다(--all은 전부)")
    s.add_argument("target")
    sub.add_parser("leave", help="대화방에서 빠진다")
    s = sub.add_parser("touch", help="모드용: 고친 파일을 최근 파일에 올린다")
    s.add_argument("path")
    s = sub.add_parser("check", help="모드용: 파일을 고치기 전에 겹침을 본다")
    s.add_argument("path")
    sub.add_parser("poll", help="모드용: 생존 신호와 새 글")
    sub.add_parser("activity", help="모드용: 사람 요청이나 턴이 있었다고 기록한다")
    sub.add_parser("yield", help="모드용: 턴이 끝나면 처리 중 표시를 내린다")
    s = sub.add_parser("take", help="멘션 없는 사용자 질문에 답하겠다고 손을 든다. 한 질문에 한 세션만 성공한다")
    s.add_argument("msg", help="그 질문의 글 id")
    s = sub.add_parser("dump", help="패널용 상태")
    s.add_argument("--quiet-hours", type=float, default=6, help="마지막 글 뒤 이만큼 조용하면 끝난 스레드로 본다")
    s.add_argument("--waiting-hours", type=float, default=24, help="답 기다림 스레드는 이만큼 지나야 끝난 것으로 본다")
    s.add_argument("--limit", type=int, default=30)
    sub.add_parser("paths", help="저장 위치")
    sub.add_parser("whoami", help="이 세션의 ID")
    a = ap.parse_args()
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
