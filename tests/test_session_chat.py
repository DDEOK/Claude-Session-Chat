"""scripts/session-chat.py의 판단을 가짜 세션으로 확인하는 검사. 표준 라이브러리만 쓴다.

실행: python3 -m unittest discover -s tests
HOME을 임시 폴더로 바꿔 돌리므로 실제 대화방 기록(~/.cache/session-chat/)은 건드리지 않는다.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "session-chat.py")
CODEX_HOOK = os.path.join(os.path.dirname(__file__), "..", "codex", "session-chat-hook.py")


class SessionChatTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = {**os.environ, "HOME": self.home.name, "USERPROFILE": self.home.name}
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.work = os.path.join(self.home.name, "work")
        os.makedirs(self.work)
        self.file = os.path.join(self.work, "notes.md")
        open(self.file, "w").close()

    def tearDown(self):
        self.home.cleanup()

    def run_chat(self, sid, *args, ok=True):
        r = subprocess.run([sys.executable, SCRIPT, "--id", sid, *args], capture_output=True, text=True, env=self.env, encoding="utf-8")
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def join(self, sid, title, doing=""):
        return json.loads(self.run_chat(sid, "profile", "--title", title, "--doing", doing, "--cwd", self.work).stdout)

    def test_overlap_when_same_place_and_same_file(self):
        self.join("aaaa0001", "메모 정리", "notes.md 할 일 정리")
        self.run_chat("aaaa0001", "touch", self.file)
        self.join("bbbb0002", "다른 일", "계산기 만들기")
        self.run_chat("bbbb0002", "touch", self.file)
        out = self.join("bbbb0002", "다른 일", "계산기 만들기")
        self.assertEqual([o["id"] for o in out["overlaps"]], ["aaaa0001"])

    def test_find_ranks_claim_over_recent_edit(self):
        self.join("aaaa0001", "메모 정리")
        self.join("bbbb0002", "메모 검토")
        self.join("cccc0003", "묻는 세션")
        self.run_chat("aaaa0001", "touch", self.file)
        self.run_chat("bbbb0002", "claim", self.file)
        found = json.loads(self.run_chat("cccc0003", "find", "notes.md").stdout)
        self.assertEqual(found[0]["id"], "bbbb0002")

    def test_check_reports_claim_by_other_but_not_own(self):
        self.join("aaaa0001", "메모 정리")
        self.join("bbbb0002", "다른 일")
        self.run_chat("aaaa0001", "claim", self.file)
        other = json.loads(self.run_chat("bbbb0002", "check", self.file).stdout)
        own = json.loads(self.run_chat("aaaa0001", "check", self.file).stdout)
        self.assertEqual(other["claimed"]["id"], "aaaa0001")
        self.assertIsNone(own["claimed"])

    def test_new_thread_needs_title_and_a_post_without_mention_is_a_note(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        self.run_chat("bbbb0002", "poll")
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "본문", ok=False).returncode, 0)
        # 멘션 없는 글은 기록이라 아무에게도 가지 않는다. 질문인데 받는 세션이 없으면 거절한다
        note = json.loads(self.run_chat("aaaa0001", "post", "--title", "작업 기록", "빌드 시작함").stdout)
        self.assertEqual(json.loads(self.run_chat("bbbb0002", "poll").stdout), [])
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["root"]["kind"], "note")
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--kind", "ask", "--thread", note["thread"], "누구든", ok=False).returncode, 0)
        # 멘션은 질문·요청이라 기록 종류로 멘션하지 못한다
        r = self.run_chat("aaaa0001", "post", "--kind", "note", "--to", "bbbb0002", "--thread", note["thread"], "알림", ok=False)
        self.assertIn("drop --to", r.stderr)

    def test_poll_delivers_only_to_recipient_once(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        self.join("cccc0003", "구경하는 세션")
        for sid in ("bbbb0002", "cccc0003"):
            self.run_chat(sid, "poll")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "첫 할 일이 뭐야?")
        got = json.loads(self.run_chat("bbbb0002", "poll").stdout)
        self.assertEqual([m["text"] for m in got], ["첫 할 일이 뭐야?"])
        self.assertEqual(got[0]["thread_title"], "질문")
        self.assertEqual(json.loads(self.run_chat("bbbb0002", "poll").stdout), [])
        self.assertEqual(json.loads(self.run_chat("cccc0003", "poll").stdout), [])

    def test_left_session_gets_nothing_until_profile_again(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        self.run_chat("bbbb0002", "poll")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        # 대화방 나가기는 사용자가 그 창에서 시켰을 때만 된다. 스레드 나가기(part)와 헷갈린 세션이 끊긴 적이 있다(2026-11-03)
        r = self.run_chat("bbbb0002", "leave", ok=False)
        self.assertIn("--user-asked", r.stderr)
        self.run_chat("bbbb0002", "leave", "--user-asked")
        self.assertEqual(json.loads(self.run_chat("bbbb0002", "poll").stdout), [])
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--thread", root["thread"], "둘", ok=False).returncode, 0)
        # 사용자가 그 세션에 요청을 보내면 다시 참여한다
        self.assertIn("Back", self.run_chat("bbbb0002", "activity", "--user").stdout)
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--thread", root["thread"], "셋")
        # 나가기 전에 와 있던 글(하나)도 돌아오면 받는다
        self.assertEqual([m["text"] for m in json.loads(self.run_chat("bbbb0002", "poll").stdout)], ["하나", "셋"])

    def test_user_comment_skips_participants_who_left(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        self.run_chat("bbbb0002", "leave", "--user-asked")
        r = self.run_chat("user", "post", "--to", "aaaa0001,bbbb0002", "--kind", "comment", "--reply-to", root["id"], "의견")
        self.assertIn("thread", json.loads(r.stdout))

    def test_subagent_id_is_a_valid_recipient(self):
        self.join("aaaa0001", "부모 세션")
        self.join("bbbb0002", "답하는 세션")
        self.run_chat("aaaa0001", "--sub", "research", "post", "--to", "bbbb0002", "--title", "조사", "질문")
        root = self.run_chat("bbbb0002", "read").stdout
        self.assertIn("질문", root)
        log = os.path.join(self.home.name, ".cache", "session-chat", "log.jsonl")
        first = json.loads(open(log, encoding="utf-8").readline())
        self.run_chat("bbbb0002", "post", "--to", "aaaa0001.research", "--kind", "reply", "--reply-to", first["id"], "--status", "answered", "답")

    def test_reply_needs_status_and_user_comment_reopens(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        self.assertNotEqual(self.run_chat("bbbb0002", "post", "--to", "aaaa0001", "--kind", "reply", "--reply-to", root["id"], "답", ok=False).returncode, 0)
        self.run_chat("bbbb0002", "post", "--to", "aaaa0001", "--kind", "reply", "--reply-to", root["id"], "--status", "answered", "답")
        state = lambda: json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["state"]
        self.assertEqual(state(), "answered")
        self.run_chat("user", "post", "--to", "aaaa0001,bbbb0002", "--kind", "comment", "--reply-to", root["id"], "마지막 것도 알려 줘")
        self.assertEqual(state(), "waiting")

    def test_thread_closes_by_quiet_time_not_by_reply(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        ans = json.loads(self.run_chat("bbbb0002", "post", "--to", "aaaa0001", "--kind", "reply", "--reply-to", root["id"], "--status", "answered", "답").stdout)
        dump = lambda *extra: json.loads(self.run_chat("aaaa0001", "dump", *extra).stdout)["threads"][0]
        self.assertFalse(dump()["closed"])
        self.assertTrue(dump("--quiet-hours", "0")["closed"])
        # 답을 기다리는 스레드는 조용한 시간 기준이 아니라 더 긴 기준을 쓴다. 이어 묻는 것은 그 스레드의 새 질문이다
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--thread", ans["thread"], "하나 더")
        self.assertFalse(dump("--quiet-hours", "0")["closed"])
        self.assertTrue(dump("--quiet-hours", "0", "--waiting-hours", "0")["closed"])

    def test_poll_survives_cursor_in_middle_of_line(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나")
        cursor = os.path.join(self.home.name, ".cache", "session-chat", "cursors", "bbbb0002")
        with open(cursor, "w") as f:
            f.write("5")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문 둘", "둘")
        got = json.loads(self.run_chat("bbbb0002", "poll").stdout)
        self.assertEqual([m["text"] for m in got], ["둘"])

    def test_reply_without_mention_goes_to_asker_only(self):
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "답하는 세션")
        self.join("cccc0003", "같은 스레드의 다른 세션")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002,cccc0003", "--title", "질문", "첫 할 일?").stdout)
        for sid in ("aaaa0001", "bbbb0002", "cccc0003"):
            self.run_chat(sid, "poll")
        # 멘션을 빠뜨린 답은 물은 쪽에게만 간다
        self.run_chat("bbbb0002", "post", "--kind", "reply", "--reply-to", root["id"], "--status", "answered", "책 반납")
        self.assertEqual([m["text"] for m in json.loads(self.run_chat("aaaa0001", "poll").stdout)], ["책 반납"])
        self.assertEqual(json.loads(self.run_chat("cccc0003", "poll").stdout), [])
        # 답이 달린 글은 서브 댓글로 원래 글을 가리킨다
        last = json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["posts"][-1]
        self.assertEqual(last["reply_to"], root["id"])

    def test_pending_mentions_and_busy(self):
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "답하는 세션")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        dump = lambda: json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]
        self.assertEqual([p["who"] for p in dump()["pending"]], ["bbbb0002"])
        self.run_chat("bbbb0002", "poll")
        self.assertEqual(dump()["busy"], ["bbbb0002"])
        # 작업 중 답(partial)은 답이 아니다. 진행 상황은 멘션 없는 기록으로 남긴다
        r = self.run_chat("bbbb0002", "post", "--reply-to", root["id"], "--status", "partial", "찾는 중", ok=False)
        self.assertIn("note", r.stderr)
        self.run_chat("bbbb0002", "post", "--thread", root["thread"], "찾는 중")
        self.assertEqual(dump()["pending"], [])
        self.run_chat("bbbb0002", "post", "--kind", "reply", "--reply-to", root["id"], "--status", "answered", "답")
        self.assertEqual(dump()["busy"], [])

    def test_open_user_question_only_one_taker_answers(self):
        self.join("aaaa0001", "세션 하나")
        self.join("bbbb0002", "세션 둘")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "스레드", "시작").stdout)
        self.run_chat("bbbb0002", "post", "--reply-to", root["id"], "--status", "answered", "알았다")
        for sid in ("aaaa0001", "bbbb0002"):
            self.run_chat(sid, "poll")
        q = json.loads(self.run_chat("user", "post", "--open", "--kind", "comment", "--reply-to", root["id"], "누가 알아?").stdout)
        # 멘션 없는 사용자 질문은 그 스레드에 글을 쓴 세션 모두가 받는다
        self.assertEqual(len(json.loads(self.run_chat("aaaa0001", "poll").stdout)), 1)
        self.assertEqual(len(json.loads(self.run_chat("bbbb0002", "poll").stdout)), 1)
        # 먼저 손 든 세션만 성공하고, 늦은 세션은 답을 올리지 못한다
        self.assertTrue(json.loads(self.run_chat("bbbb0002", "take", q["id"]).stdout)["ok"])
        self.assertFalse(json.loads(self.run_chat("aaaa0001", "take", q["id"]).stdout)["ok"])
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--to", "user", "--kind", "reply", "--reply-to", q["id"], "--status", "answered", "나도", ok=False).returncode, 0)
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["open"]["taker"], "bbbb0002")

    def test_open_question_without_take_still_allows_only_first_answer(self):
        self.join("aaaa0001", "세션 하나")
        self.join("bbbb0002", "세션 둘")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "스레드", "시작").stdout)
        q = json.loads(self.run_chat("user", "post", "--open", "--reply-to", root["id"], "정리해 줘").stdout)
        # 둘 다 손을 안 들고 바로 답해도 먼저 올린 하나만 성공한다(실측: 둘 다 답했다)
        self.run_chat("aaaa0001", "post", "--to", "user", "--reply-to", q["id"], "--status", "answered", "정리 하나")
        self.assertNotEqual(self.run_chat("bbbb0002", "post", "--to", "user", "--reply-to", q["id"], "--status", "answered", "정리 둘", ok=False).returncode, 0)
        self.assertIsNone(json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["open"])

    def test_reply_kind_defaults_to_reply(self):
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "답하는 세션")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        # 종류를 안 적은 답은 질문이 아니라 답이다. 상태가 없으면 알려 준다
        self.assertNotEqual(self.run_chat("bbbb0002", "post", "--reply-to", root["id"], "답", ok=False).returncode, 0)
        self.run_chat("bbbb0002", "post", "--reply-to", root["id"], "--status", "answered", "답")
        t = json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]
        self.assertEqual(t["posts"][0]["kind"], "reply")
        self.assertEqual(t["pending"], [])

    def test_sessions_cannot_ask_without_mention(self):
        self.join("aaaa0001", "세션 하나")
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--open", "--title", "질문", "누구든", ok=False).returncode, 0)

    def test_claim_is_all_or_nothing(self):
        self.join("aaaa0001", "먼저 맡은 세션")
        self.join("bbbb0002", "나중 세션")
        other = os.path.join(self.work, "other.md")
        open(other, "w").close()
        self.run_chat("aaaa0001", "claim", self.file)
        self.assertNotEqual(self.run_chat("bbbb0002", "claim", other, self.file, ok=False).returncode, 0)
        board = json.loads(self.run_chat("bbbb0002", "dump").stdout)["board"]
        self.assertEqual([c["by"] for c in board], ["aaaa0001"])

    def test_serve_is_read_only_and_local(self):
        import socket
        import urllib.error
        import urllib.request

        self.join("aaaa0001", "웹 시험 세션")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([sys.executable, SCRIPT, "--lang", "ko", "serve", "--port", str(port)], stdout=subprocess.PIPE, env=self.env, text=True)
        try:
            self.assertIn(f"127.0.0.1:{port}", proc.stdout.readline())
            base = f"http://127.0.0.1:{port}"
            state = json.loads(urllib.request.urlopen(f"{base}/state.json").read())
            self.assertEqual(state["lang"], "ko")
            self.assertIn("aaaa0001", state["sessions"])
            self.assertIn("<html", urllib.request.urlopen(f"{base}/").read().decode())
            # 글을 받지 않고, 정해진 파일 밖은 내주지 않는다
            for req in (urllib.request.Request(f"{base}/state.json", data=b"x", method="POST"), f"{base}/../scripts/session-chat.py"):
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(req)
        finally:
            proc.terminate()
            proc.wait()
            proc.stdout.close()

    def test_serve_detach_reuses_one_server_and_stops_when_unwatched(self):
        import time
        import urllib.request

        web = os.path.join(self.home.name, ".cache", "session-chat", "web.json")
        first = self.run_chat("aaaa0001", "serve", "--detach").stdout.strip()
        rec = json.load(open(web, encoding="utf-8"))
        try:
            # 두 번째 호출은 새로 띄우지 않고 같은 서버를 돌려준다. 그때의 언어는 서버를 다시 띄우지 않고 반영된다
            self.assertEqual(self.run_chat("bbbb0002", "--lang", "ko", "serve", "--detach").stdout.strip(), first)
            self.assertEqual(json.load(open(web, encoding="utf-8"))["pid"], rec["pid"])
            state = json.loads(urllib.request.urlopen(f"{first}state.json").read())
            self.assertEqual(state["token"], rec["token"])
            self.assertEqual(state["lang"], "ko")
        finally:
            os.kill(rec["pid"], 15)
        # 꺼진 뒤 다시 부르면 같은 번호로 새 서버가 뜬다
        time.sleep(0.3)
        again = self.run_chat("aaaa0001", "serve", "--detach").stdout.strip()
        rec2 = json.load(open(web, encoding="utf-8"))
        os.kill(rec2["pid"], 15)
        self.assertEqual(again, first)
        self.assertNotEqual(rec2["token"], rec["token"])

    def test_serve_stops_when_no_page_asks(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([sys.executable, SCRIPT, "serve", "--port", str(port), "--idle", "1"], stdout=subprocess.DEVNULL, env=self.env)
        try:
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
        web = json.load(open(os.path.join(self.home.name, ".cache", "session-chat", "web.json"), encoding="utf-8"))
        # 번호는 남기고 표식은 지운다
        self.assertEqual(web, {"port": port})

    def test_new_thread_with_same_sessions_points_to_open_one(self):
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "답하는 세션")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--kind", "ask", "--title", "첫 주제", "질문 하나")
        r = self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--kind", "ask", "--title", "둘째 주제", "질문 둘")
        # 막지는 않고, 열린 스레드를 알려 준다
        self.assertIn("첫 주제", r.stderr)
        self.assertEqual(len(json.load(open(os.path.join(self.home.name, ".cache", "session-chat", "threads.json"), encoding="utf-8"))), 2)

    def test_archive_moves_old_threads_and_keeps_cursors(self):
        import datetime

        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        self.run_chat("bbbb0002", "poll")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--kind", "ask", "--title", "옛 스레드", "옛 질문")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--kind", "ask", "--title", "새 스레드", "새 질문")
        root = os.path.join(self.home.name, ".cache", "session-chat")
        log = os.path.join(root, "log.jsonl")
        rows = [json.loads(line) for line in open(log, encoding="utf-8")]
        # 첫 스레드의 글을 열흘 전 것으로 바꾼다
        old_ts = (datetime.datetime.now().astimezone() - datetime.timedelta(days=10)).isoformat(timespec="seconds")
        rows[0]["ts"] = old_ts
        with open(log, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        # 정리는 한 시간에 한 번만 돈다. 앞선 poll이 남긴 표식을 지워 이번에 돌게 한다
        os.remove(os.path.join(root, "archive", ".stamp"))
        got = json.loads(self.run_chat("bbbb0002", "poll", "--archive-days", "7").stdout)
        # 옛 스레드는 보관 파일로 가고, 받는 세션은 남은 새 글을 그대로 받는다
        self.assertEqual([m["text"] for m in got], ["새 질문"])
        self.assertEqual([json.loads(line)["text"] for line in open(log, encoding="utf-8")], ["새 질문"])
        archived = open(os.path.join(root, "archive", f"{old_ts[:7]}.jsonl"), encoding="utf-8").read()
        self.assertIn("옛 질문", archived)
        threads = json.load(open(os.path.join(root, "threads.json"), encoding="utf-8"))
        self.assertEqual([t["title"] for t in threads.values()], ["새 스레드"])
        # 커서가 새 끝을 가리켜 다시 받지 않는다
        self.assertEqual(json.loads(self.run_chat("bbbb0002", "poll").stdout), [])

    def test_release_several_and_all(self):
        self.join("aaaa0001", "맡는 세션")
        other = os.path.join(self.work, "other.md")
        third = os.path.join(self.work, "third.md")
        for p in (other, third):
            open(p, "w").close()
        self.run_chat("aaaa0001", "claim", self.file, other, third)
        self.run_chat("aaaa0001", "release", self.file, other)
        board = json.loads(self.run_chat("aaaa0001", "dump").stdout)["board"]
        self.assertEqual([c["target"] for c in board], [os.path.abspath(third)])
        # --all은 옵션이다. 위치 인자로 받던 때는 argparse가 실패했다
        self.run_chat("aaaa0001", "release", "--all")
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "dump").stdout)["board"], [])

    def test_asker_wakes_once_when_everyone_asked_has_answered(self):
        # 기록은 아무에게도 가지 않고, 여러 세션에 물은 질문은 모두 답했을 때 물은 세션을 한 번 깨운다(2026-11-03 사용자 결정)
        for sid, title in (("aaaa0001", "묻는 세션"), ("bbbb0002", "답하는 세션"), ("cccc0003", "같이 묻는 세션")):
            self.join(sid, title)
            self.run_chat(sid, "poll")
        ask = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002,cccc0003", "--title", "빌드", "빌드 언제 끝나?").stdout)
        self.assertEqual([m["quiet"] for m in json.loads(self.run_chat("bbbb0002", "poll").stdout)], [False])
        self.run_chat("cccc0003", "poll")
        self.run_chat("bbbb0002", "post", "--thread", ask["thread"], "unity-batch 잡음.")
        self.run_chat("bbbb0002", "post", "--reply-to", ask["id"], "--status", "answered", "10분 뒤.")
        self.assertEqual([(m["text"], m["quiet"]) for m in json.loads(self.run_chat("aaaa0001", "poll").stdout)], [("10분 뒤.", True)])
        self.run_chat("cccc0003", "post", "--reply-to", ask["id"], "--status", "answered", "내 쪽은 끝남.")
        self.assertEqual([(m["text"], m["quiet"]) for m in json.loads(self.run_chat("aaaa0001", "poll").stdout)], [("내 쪽은 끝남.", False)])
        # 답은 물은 세션에게만 간다. 같이 멘션된 세션은 서로의 답을 받지 않는다
        self.assertEqual(json.loads(self.run_chat("cccc0003", "poll").stdout), [])

    def test_threads_have_no_membership(self):
        for sid, title in (("aaaa0001", "묻는 세션"), ("bbbb0002", "답하는 세션"), ("cccc0003", "처음 보는 세션")):
            self.join(sid, title)
            self.run_chat(sid, "poll")
        ask = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "Unity 빌드", "빌드 써도 돼?").stdout)
        # 옛 명령은 실패하지 않고 할 일이 없다고 알린다
        self.assertIn("no membership", self.run_chat("cccc0003", "join", ask["thread"]).stdout)
        self.assertIn("빌드 써도 돼?", self.run_chat("cccc0003", "show", "unity 빌드").stdout)
        self.run_chat("bbbb0002", "post", "--reply-to", ask["id"], "--status", "answered", "써도 된다.")
        # 참여자는 글을 쓴 세션뿐이다. 글을 쓴 적 없는 세션도 멘션하면 받는다
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["participants"], ["aaaa0001", "bbbb0002"])
        self.run_chat("bbbb0002", "post", "--thread", ask["thread"], "--to", "cccc0003", "단말도 쓸 건가?")
        self.assertEqual([m["text"] for m in json.loads(self.run_chat("cccc0003", "poll").stdout)], ["단말도 쓸 건가?"])

    def test_reply_rules_keep_answers_under_questions(self):
        # 한 스레드의 글이 모두 첫 질문의 답글로 쌓이던 것을 막는다(2026-11-03 사용자 지적). 답글 자리는 답만 쓴다
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "답하는 세션")
        ask = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "빌드", "빌드 언제 끝나?").stdout)
        self.assertIn("own post", self.run_chat("aaaa0001", "post", "--reply-to", ask["id"], "--status", "answered", "덧붙임.", ok=False).stderr)
        # 옛 판처럼 기록을 답글로 달면 그 스레드의 새 기록으로 받는다
        self.run_chat("bbbb0002", "post", "--kind", "info", "--reply-to", ask["id"], "unity-batch 잡음.")
        posts = json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["posts"]
        self.assertEqual((posts[-1]["kind"], posts[-1].get("top"), posts[-1].get("reply_to")), ("note", True, None))
        ans = json.loads(self.run_chat("bbbb0002", "post", "--reply-to", ask["id"], "--status", "answered", "10분 뒤.").stdout)
        # 답에 답을 달지 않는다. 확인만 하려면 아무것도 올리지 않는다
        self.assertIn("post nothing", self.run_chat("aaaa0001", "post", "--reply-to", ans["id"], "--status", "answered", "알았다.", ok=False).stderr)
        # 이어 물을 것은 그 답의 작성자를 부르는 새 질문이다(옛 판의 --kind ask --reply-to도 그렇게 받는다)
        self.run_chat("bbbb0002", "poll")
        self.run_chat("aaaa0001", "post", "--kind", "ask", "--reply-to", ans["id"], "테스트도 포함인가?")
        got = json.loads(self.run_chat("bbbb0002", "poll").stdout)
        self.assertEqual([(m["text"], m["quiet"]) for m in got], [("테스트도 포함인가?", False)])
        # 답은 물은 세션에게만 돌아간다
        self.assertIn("goes back", self.run_chat("bbbb0002", "post", "--reply-to", got[0]["id"], "--to", "cccc0003", "--status", "answered", "포함.", ok=False).stderr)

    def test_post_tells_when_the_mentioned_session_is_off(self):
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "꺼진 세션")
        os.remove(os.path.join(self.home.name, ".cache", "session-chat", "alive", "bbbb0002"))
        r = self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나")
        self.assertIn("not running", r.stderr)
        self.assertEqual(json.loads(r.stdout)["offline"], ["bbbb0002"])

    def test_answer_deadline_counts_from_receipt_and_tells_once(self):
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "바쁜 세션")
        self.run_chat("aaaa0001", "poll")
        ask = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        # 받기 전에는 기한을 세지 않는다
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "poll", "--answer-minutes", "0.001").stdout), [])
        self.run_chat("bbbb0002", "seen", ask["id"])
        import time
        time.sleep(0.2)
        got = json.loads(self.run_chat("aaaa0001", "poll", "--answer-minutes", "0.001").stdout)
        self.assertEqual([(m["kind"], m["quiet"]) for m in got], [("notice", False)])
        self.assertIn("No answer yet", got[0]["text"])
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "poll", "--answer-minutes", "0.001").stdout), [])

    def test_claim_lapses_without_renewal(self):
        self.join("aaaa0001", "멈춘 세션")
        self.join("bbbb0002", "다음 세션")
        self.run_chat("aaaa0001", "claim", "unity-batch")
        self.assertNotEqual(self.run_chat("bbbb0002", "claim", "unity-batch", ok=False).returncode, 0)
        board = os.path.join(self.home.name, ".cache", "session-chat", "board.json")
        data = json.load(open(board, encoding="utf-8"))
        data["unity-batch"]["renewed"] -= 3600
        json.dump(data, open(board, "w", encoding="utf-8"))
        self.run_chat("bbbb0002", "claim", "unity-batch")
        # 연장하면 다시 유효해진다
        self.run_chat("bbbb0002", "renew")
        self.assertNotEqual(self.run_chat("aaaa0001", "claim", "unity-batch", ok=False).returncode, 0)

    def test_waits_hand_over_in_order_one_at_a_time(self):
        for sid, title in (("aaaa0001", "쓰는 세션"), ("bbbb0002", "먼저 기다린 세션"), ("cccc0003", "나중에 기다린 세션")):
            self.join(sid, title)
            self.run_chat(sid, "poll")
        self.run_chat("aaaa0001", "claim", "s22")
        self.run_chat("bbbb0002", "claim", "s22", "--wait", ok=False)
        self.assertIn("1 ahead", self.run_chat("cccc0003", "claim", "s22", "--wait", ok=False).stderr)
        self.run_chat("aaaa0001", "release", "s22")
        # 나중에 기다린 세션은 먼저 기다린 세션이 넘겨받을 때까지 알림을 받지 않는다
        self.assertEqual(json.loads(self.run_chat("cccc0003", "poll").stdout), [])
        self.assertEqual([m["kind"] for m in json.loads(self.run_chat("bbbb0002", "poll").stdout)], ["notice"])
        # 맡아 둔 동안은 다른 세션이 잡지 못한다
        self.assertNotEqual(self.run_chat("cccc0003", "claim", "s22", ok=False).returncode, 0)
        # 5분 안에 잡지 않으면 다음 세션에 넘어간다
        path = os.path.join(self.home.name, ".cache", "session-chat", "waits.json")
        waits = json.load(open(path, encoding="utf-8"))
        waits[0]["offered"] -= 600
        json.dump(waits, open(path, "w", encoding="utf-8"))
        self.assertEqual([m["kind"] for m in json.loads(self.run_chat("cccc0003", "poll").stdout)], ["notice"])
        self.run_chat("cccc0003", "claim", "s22")

    def test_outbox_lists_waking_posts_for_sessions_in_a_turn(self):
        # 작업 중인 세션에는 Claude Code 자체 세션 메시지로도 보낸다. 권한 모드 갈래를 둘 다 알고 다르면 보내지 않는다(승인을 기다리다 버려진다, 2026-11-03 시험)
        self.run_chat("aaaa0001", "profile", "--title", "묻는 세션", "--full-id", "aaaa0001-full")
        self.run_chat("bbbb0002", "profile", "--title", "일하는 세션", "--full-id", "bbbb0002-full")
        self.run_chat("cccc0003", "profile", "--title", "모드가 다른 세션", "--full-id", "cccc0003-full")
        self.run_chat("aaaa0001", "activity", "--mode", "bypass")
        self.run_chat("bbbb0002", "activity", "--turn", "on", "--mode", "bypass")
        self.run_chat("cccc0003", "activity", "--turn", "on", "--mode", "prompt")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002,cccc0003", "--title", "질문", "하나")
        self.run_chat("dddd0004", "profile", "--title", "모드를 모르는 세션", "--full-id", "dddd0004-full")
        self.run_chat("dddd0004", "activity", "--turn", "on")
        self.run_chat("aaaa0001", "post", "--to", "dddd0004", "--title", "질문 셋", "셋")
        out = json.loads(self.run_chat("aaaa0001", "outbox").stdout)
        self.assertEqual([o["to"] for o in out], [["bbbb0002-full"], ["dddd0004-full"]])
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "outbox").stdout), [])
        # 턴이 끝나면(yield) 작업 중 표시가 내려간다
        self.run_chat("bbbb0002", "yield")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문 둘", "둘")
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "outbox").stdout), [])

    def test_related_matches_words_glued_in_korean(self):
        # 사용자 요청의 낱말이 스레드 제목·다른 세션의 하는 일과 겹치면 알린다. 한국어는 붙여 쓰므로(`engine관련`) 글자 종류로 끊는다(2026-11-03)
        self.join("aaaa0001", "엔진 조사")
        self.join("bbbb0002", "결제 검토")
        self.run_chat("bbbb0002", "profile", "--doing", "Engine 결제 연동 검토")
        tid = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "Engine 조사 범위", "범위 알려 달라").stdout)["thread"]
        out = json.loads(self.run_chat("cccc0003", "related", "engine관련 정리할 게 있을까?").stdout)
        self.assertEqual([t["id"] for t in out["threads"]], [tid])
        self.assertEqual([s["id"] for s in out["sessions"]], ["bbbb0002"])
        # 흔한 낱말이나 두 글자 한글 낱말 하나만 겹치면 알리지 않는다
        self.assertEqual(json.loads(self.run_chat("cccc0003", "related", "관련 정보 정리").stdout), {"threads": [], "sessions": []})
        self.assertEqual(json.loads(self.run_chat("cccc0003", "related", "결제대로 고쳐").stdout), {"threads": [], "sessions": []})
        # 주제로 찾으면 글을 쓴 적 없는 세션도 그 스레드를 본다. 그냥 threads는 자기 스레드만 보인다
        self.assertIn(tid, self.run_chat("cccc0003", "threads", "--about", "engine").stdout)
        self.assertNotIn(tid, self.run_chat("cccc0003", "threads").stdout)

    def test_search_finds_an_answer_that_show_leaves_out(self):
        # 앞 글에만 있는 답은 show 기본 8개에서 빠진다. show는 생략을 알리고 search는 본문에서 찾는다(2026-11-03 실측)
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "답한 세션")
        tid = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "엔진 조사", "근거가 뭐야?").stdout)["thread"]
        self.run_chat("bbbb0002", "post", "--thread", tid, "리플렉션은 심사 위험이라 브리지를 쓴다")
        for i in range(8):
            self.run_chat("aaaa0001", "post", "--thread", tid, f"다른 이야기 {i}")
        show = self.run_chat("cccc0003", "show", tid).stdout
        self.assertNotIn("리플렉션", show)
        self.assertIn("Showing the last 8 of 10 posts", show)
        self.assertIn("리플렉션", self.run_chat("cccc0003", "show", tid, "--last", "0").stdout)
        found = self.run_chat("cccc0003", "search", "리플렉션 근거").stdout
        self.assertIn("리플렉션은 심사 위험", found)
        self.assertIn("No posts contain", self.run_chat("cccc0003", "search", "없는낱말").stdout)

    def test_closed_session_can_be_invited_and_mentioned(self):
        # 꺼진 세션도 초대·멘션된다. 다시 켜지면 그사이 온 글을 받는다(2026-11-03 사용자 결정)
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "꺼진 세션")
        self.run_chat("bbbb0002", "poll")
        os.remove(os.path.join(self.home.name, ".cache", "session-chat", "alive", "bbbb0002"))
        ask = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "이어서", "멈춘 작업 이어 줄 수 있나?").stdout)
        self.run_chat("user", "post", "--to", "bbbb0002", "--kind", "comment", "--reply-to", ask["id"], "부탁해")
        self.assertEqual([m["text"] for m in json.loads(self.run_chat("bbbb0002", "poll").stdout)], ["멈춘 작업 이어 줄 수 있나?", "부탁해"])

    def test_claim_wait_gets_one_notice_when_free(self):
        self.join("aaaa0001", "쓰는 세션")
        self.join("bbbb0002", "기다리는 세션")
        self.run_chat("bbbb0002", "poll")
        self.run_chat("aaaa0001", "claim", "unity-batch")
        r = self.run_chat("bbbb0002", "claim", "unity-batch", "--wait", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("wait list", r.stderr)
        self.assertEqual(json.loads(self.run_chat("bbbb0002", "poll").stdout), [])
        self.run_chat("aaaa0001", "release", "unity-batch")
        got = json.loads(self.run_chat("bbbb0002", "poll").stdout)
        self.assertEqual([(m["kind"], m["quiet"]) for m in got], [("notice", False)])
        self.assertIn("unity-batch", got[0]["text"])
        self.assertEqual(json.loads(self.run_chat("bbbb0002", "poll").stdout), [])
        self.run_chat("bbbb0002", "claim", "unity-batch")

    def test_claim_wait_notices_when_holder_leaves(self):
        self.join("aaaa0001", "쓰는 세션")
        self.join("bbbb0002", "기다리는 세션")
        self.run_chat("bbbb0002", "poll")
        self.run_chat("aaaa0001", "claim", "device-s22")
        self.run_chat("bbbb0002", "claim", "device-s22", "--wait", ok=False)
        self.run_chat("aaaa0001", "leave", "--user-asked")
        got = json.loads(self.run_chat("bbbb0002", "poll").stdout)
        self.assertEqual([m["kind"] for m in got], ["notice"])

    def test_yield_clears_busy_when_turn_ends_without_answer(self):
        self.join("aaaa0001", "묻는 세션")
        self.join("bbbb0002", "답하는 세션")
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나")
        self.run_chat("bbbb0002", "poll")
        self.run_chat("bbbb0002", "yield")
        self.assertEqual(json.loads(self.run_chat("aaaa0001", "dump").stdout)["threads"][0]["busy"], [])

    def test_auto_summary_keeps_app_title_and_recent_manual_edit(self):
        self.run_chat("aaaa0001", "profile", "--title", "주간 할 일 목록", "--app-title", "--cwd", self.work)
        self.run_chat("aaaa0001", "profile", "--auto", "--title", "엉뚱한 이름", "--doing", "자동 요약")
        who = json.loads(self.run_chat("aaaa0001", "who", "--json").stdout)
        self.assertEqual(who["aaaa0001"]["title"], "주간 할 일 목록")

    def codex_env(self):
        """가짜 codex를 PATH 앞에 둔다. 받은 인자를 한 줄씩 기록해 codex queue 호출을 확인한다."""
        bin_dir = os.path.join(self.home.name, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        fake = os.path.join(bin_dir, "codex")
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"{self.home.name}/codex-calls\"\n")
        os.chmod(fake, 0o755)
        return {**self.env, "PATH": bin_dir + os.pathsep + self.env.get("PATH", "")}

    def codex_sid(self):
        with open(os.path.join(self.home.name, ".cache", "session-chat", "sessions.json"), encoding="utf-8") as f:
            sessions = json.load(f)
        return next(k for k, v in sessions.items() if v.get("runtime") == "codex"), sessions

    def transcript(self, full, originator="codex-tui", source="vscode"):
        """Codex가 세션 기록 파일 첫 줄에 남기는 출처. 훅은 이것으로 일회성 실행(codex exec)을 가린다."""
        path = os.path.join(self.home.name, f"rollout-{full}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": full, "originator": originator, "source": source}}) + "\n")
        return path

    def hook(self, env, event, **fields):
        if "session_id" in fields and "transcript_path" not in fields:
            fields["transcript_path"] = self.transcript(fields["session_id"])
        r = subprocess.run([sys.executable, CODEX_HOOK], input=json.dumps({"hook_event_name": event, "cwd": self.work, **fields}), capture_output=True, text=True, env=env, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_codex_session_joins_gets_posts_and_is_woken_by_codex_queue(self):
        # Codex 세션: 시작 훅이 참여시키고, 질문은 codex queue로 깨우며, 요청 앞에 새 글을 붙인다(2026-11-06)
        env = self.codex_env()
        full = "01a1b037-3e48-7303-829b-1f47ee40713b"
        out = self.hook(env, "SessionStart", session_id=full, source="startup")
        self.assertIn("# Session chat (session-chat)", out["hookSpecificOutput"]["additionalContext"])
        cx, sessions = self.codex_sid()
        self.assertEqual(sessions[cx]["full_id"], full)
        self.join("aaaa0001", "묻는 세션")
        r = subprocess.run([sys.executable, SCRIPT, "--id", "aaaa0001", "post", "--to", cx, "--title", "Codex에게", "2+2는?"], capture_output=True, text=True, env=env, encoding="utf-8")
        self.assertEqual(json.loads(r.stdout)["offline"], [])
        ask = json.loads(r.stdout)["id"]
        for _ in range(50):
            if os.path.exists(os.path.join(self.home.name, "codex-calls")):
                break
            time.sleep(0.1)
        with open(os.path.join(self.home.name, "codex-calls"), encoding="utf-8") as f:
            calls = f.read().split("\n")
        self.assertEqual(calls[:3], ["queue", "--thread", full])
        self.assertIn("--resume", calls)
        # 요청 앞에 붙이되, codex queue로 이미 요청에 들어온 글은 다시 붙이지 않는다
        queued = self.hook(env, "UserPromptSubmit", session_id=full, turn_id="t1", prompt=f"[Session chat · thread 'Codex에게' ({ask}) · post {ask} from 묻는 세션 (aaaa0001)]\n2+2는?")
        self.assertIsNone(queued)
        # Codex 셸에서는 CODEX_THREAD_ID로 자기 ID를 찾는다. Claude Code 안에서 띄워 CLAUDE_CODE_SESSION_ID를 물려받아도 Codex 쪽이 먼저다
        r = subprocess.run([sys.executable, SCRIPT, "post", "--reply-to", ask, "--status", "answered", "4"], capture_output=True, text=True, env={**env, "CODEX_THREAD_ID": full, "CLAUDE_CODE_SESSION_ID": "ffff0000-claude"}, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(self.run_chat("aaaa0001", "poll").stdout)
        self.assertEqual([(m["from"], m["text"]) for m in got], [(cx, "4")])

    def test_codex_exec_and_ephemeral_runs_do_not_join(self):
        # 다른 도구가 돌린 codex exec가 실행마다 패널에 쌓였다(2026-11-06). 일회성 실행과 기록 없는 세션은 넣지 않는다
        env = self.codex_env()
        full = "01a1b03b-df6b-7042-9a9b-3a37a6969a2e"
        exec_log = self.transcript(full, originator="codex_exec", source="exec")
        self.assertIsNone(self.hook(env, "SessionStart", session_id=full, source="startup", transcript_path=exec_log))
        self.assertIsNone(self.hook(env, "UserPromptSubmit", session_id=full, turn_id="t1", prompt="make an image", transcript_path=exec_log))
        self.assertIsNone(self.hook(env, "SessionStart", session_id="01a1b03b-0000-7000-8000-000000000009", source="startup", transcript_path=None))
        sessions_file = os.path.join(self.home.name, ".cache", "session-chat", "sessions.json")
        self.assertFalse(os.path.exists(sessions_file) and any(v.get("runtime") == "codex" for v in json.load(open(sessions_file, encoding="utf-8")).values()))

    def test_codex_hook_stops_patch_on_claimed_file_and_continues_turn_for_new_ask(self):
        env = self.codex_env()
        full = "01a1b03b-3fe7-7ba3-9985-b419021bfade"
        self.hook(env, "SessionStart", session_id=full, source="startup")
        self.join("aaaa0001", "잡은 세션")
        self.run_chat("aaaa0001", "claim", self.file)
        patch = f"*** Begin Patch\n*** Update File: {os.path.basename(self.file)}\n@@\n-a\n+b\n*** End Patch"
        out = self.hook(env, "PreToolUse", session_id=full, turn_id="t1", tool_name="apply_patch", tool_input={"command": patch})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("aaaa0001", out["hookSpecificOutput"]["permissionDecisionReason"])
        # 턴 도중 온 질문은 끝내지 말고 답하게 한다. 이어 간 턴(stop_hook_active)에서는 다시 막지 않는다
        cx, _ = self.codex_sid()
        subprocess.run([sys.executable, SCRIPT, "--id", "aaaa0001", "post", "--to", cx, "--title", "도중 질문", "지금 뭐 해?"], capture_output=True, env=env)
        out = self.hook(env, "Stop", session_id=full, turn_id="t1", stop_hook_active=False)
        self.assertEqual(out["decision"], "block")
        self.assertIn("지금 뭐 해?", out["reason"])
        self.assertIsNone(self.hook(env, "Stop", session_id=full, turn_id="t1", stop_hook_active=True))


if __name__ == "__main__":
    unittest.main()
