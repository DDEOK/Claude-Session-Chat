---
name: chat-room-web
description: Open the session chat as a read-only web page in the browser, so the user can see the threads and sessions (Claude Code and Codex) shared on this machine. Codex has no session chat panel, so this is how the user looks at the chat from Codex. Use when the user types $chat-room-web, or says "show the session chat", "open the chat room page", "세션 대화방 보여 줘", "대화방 웹으로 열어 줘", "대화방 페이지 열어".
---

# Open the session chat page

1. Run the session chat script with `serve --detach`. The script is `scripts/session-chat.py` in the plugin folder, three folders above this `SKILL.md` (`<plugin>/codex/skills/chat-room-web/SKILL.md` → `<plugin>/scripts/session-chat.py`). The session-chat guide added at session start also names the exact command.

   ```sh
   python3 "<plugin>/scripts/session-chat.py" serve --detach
   ```

   It starts the page server apart from this session, or reuses the one already running, prints the address (`http://127.0.0.1:<port>/`) and returns at once.
2. Open the address in the browser: `open <address>` on macOS, `xdg-open <address>` on Linux, `start <address>` on Windows.
3. Tell the user the address in one line.

If the sandbox stops the server from starting or the browser from opening, ask the user to allow that command once.

The page is read-only and refreshes by itself. Comments to a thread are written in the Claude Code panel (`/chat-room`), not on this page. The server stops by itself five minutes after no page has asked for updates.
