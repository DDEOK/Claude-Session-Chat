"""scripts/session-chat.py의 판단을 가짜 세션으로 확인하는 검사. 표준 라이브러리만 쓴다.

실행: python3 -m unittest discover -s tests
HOME을 임시 폴더로 바꿔 돌리므로 실제 대화방 기록(~/.cache/session-chat/)은 건드리지 않는다.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "session-chat.py")


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

    def test_new_thread_needs_title_and_recipient(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "본문", ok=False).returncode, 0)
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--to", "", "--title", "제목", "본문", ok=False).returncode, 0)

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
        self.run_chat("bbbb0002", "leave")
        self.assertEqual(json.loads(self.run_chat("bbbb0002", "poll").stdout), [])
        self.assertNotEqual(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--reply-to", root["id"], "--kind", "info", "둘", ok=False).returncode, 0)

    def test_user_comment_skips_participants_who_left(self):
        self.join("aaaa0001", "보내는 세션")
        self.join("bbbb0002", "받는 세션")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "질문", "하나").stdout)
        self.run_chat("bbbb0002", "leave")
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
        self.run_chat("bbbb0002", "post", "--to", "aaaa0001", "--kind", "reply", "--reply-to", root["id"], "--status", "answered", "답")
        dump = lambda *extra: json.loads(self.run_chat("aaaa0001", "dump", *extra).stdout)["threads"][0]
        self.assertFalse(dump()["closed"])
        self.assertTrue(dump("--quiet-hours", "0")["closed"])
        # 답을 기다리는 스레드는 조용한 시간 기준이 아니라 더 긴 기준을 쓴다
        self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--kind", "ask", "--reply-to", root["id"], "하나 더")
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
        # 진행 알림(partial)을 올려도 아직 처리 중이다
        self.run_chat("bbbb0002", "post", "--kind", "reply", "--reply-to", root["id"], "--status", "partial", "찾는 중")
        self.assertEqual(dump()["busy"], ["bbbb0002"])
        self.assertEqual(dump()["pending"], [])
        self.run_chat("bbbb0002", "post", "--kind", "reply", "--reply-to", root["id"], "--status", "answered", "답")
        self.assertEqual(dump()["busy"], [])

    def test_open_user_question_only_one_taker_answers(self):
        self.join("aaaa0001", "세션 하나")
        self.join("bbbb0002", "세션 둘")
        root = json.loads(self.run_chat("aaaa0001", "post", "--to", "bbbb0002", "--title", "스레드", "시작").stdout)
        for sid in ("aaaa0001", "bbbb0002"):
            self.run_chat(sid, "poll")
        q = json.loads(self.run_chat("user", "post", "--open", "--kind", "comment", "--reply-to", root["id"], "누가 알아?").stdout)
        # 멘션 없는 질문은 스레드 세션 모두가 받는다
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


if __name__ == "__main__":
    unittest.main()
