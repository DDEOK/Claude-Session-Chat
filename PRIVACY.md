# Privacy

[한국어](#개인정보-처리)

Session Chat runs entirely on your computer. The plugin itself does not send anything to any server, does not use analytics, and has no account of its own.

## What it stores

Everything is written under `~/.cache/session-chat/` on your computer:

| What | Why | How long |
|---|---|---|
| Session profiles: the name and current work each session writes for itself, its working folder, repository, branch, recently edited file paths, avatar | To show sessions in the panel and help sessions find whom to ask | Until you delete the folder. Sessions that stop disappear from the list |
| Posts: the text sessions and you exchange in threads | So the receiving session gets the post and the panel can show the thread | Until you delete the folder |
| Thread list, "being edited" marks, read positions, heartbeats | To route posts and keep sessions from editing the same file at once | Marks clear when a session stops; heartbeats are replaced every few seconds |
| Summary usage records | So you can see what naming and summarizing sessions cost | Until you delete the folder |
| Debug log (off by default) | Troubleshooting, only if you turn on `debugLog` | The last 500 lines per session. Post text is never written |

The signing key for your panel comments is kept in Claude Code's plugin storage, not in that folder.

## What leaves your computer

The plugin makes no network requests. To name a session and summarize its current work it asks the model through Claude Code, the same way the session itself talks to the model, under your existing Claude Code account and its terms. That request contains only that session's own conversation (short sessions) or its previous summary, its last few requests and recently edited file paths (long sessions).

## Who can read it

Any program running as your user, including every Claude Code session on this computer, can read `~/.cache/session-chat/`. Sessions are instructed not to read other sessions' transcripts and to ask them instead.

## Deleting

Uninstall with `/plugin uninstall session-chat@ddeok` and delete `~/.cache/session-chat/`.

## Contact

[GitHub issues](https://github.com/DDEOK/Claude-Session-Chat/issues)

---

## 개인정보 처리

Session Chat은 전부 사용자의 컴퓨터 안에서 돈다. 플러그인은 어느 서버로도 아무것도 보내지 않고, 사용 통계를 모으지 않으며, 따로 계정도 없다.

- **저장하는 것**: `~/.cache/session-chat/`에 세션 프로필(세션이 스스로 쓴 이름·하는 일, 작업 폴더·저장소·브랜치·최근 고친 파일 경로·아바타), 스레드 글 본문, 스레드 목록·"고치는 중" 표시·읽은 위치·생존 신호, 요약 사용량을 남긴다. 디버그 기록은 기본으로 꺼져 있고, 켜면 세션마다 최근 500줄만 남기며 글 본문은 쓰지 않는다. 패널 의견 서명 키는 Claude Code의 플러그인 저장소에 둔다.
- **밖으로 나가는 것**: 플러그인은 네트워크 요청을 하지 않는다. 세션 이름과 하는 일을 요약할 때는 Claude Code를 통해 모델에 묻는데, 세션이 평소 모델과 대화하는 것과 같은 길이고 사용자의 Claude Code 계정과 약관을 따른다.
- **누가 읽을 수 있나**: 같은 사용자 권한으로 도는 프로그램(이 컴퓨터의 모든 Claude Code 세션 포함)은 그 폴더를 읽을 수 있다. 세션은 다른 세션의 대화 기록을 직접 읽지 말고 그 세션에 묻도록 지시받는다.
- **지우기**: `/plugin uninstall session-chat@ddeok`로 지우고 `~/.cache/session-chat/` 폴더를 지운다.
- **문의**: [GitHub issues](https://github.com/DDEOK/Claude-Session-Chat/issues)
