# Claude Session Chat

[한국어](README.ko.md)

A Claude Code plugin that lets sessions open at the same time on one computer ask each other for information and work in threads, and answer.
A mod running in every session delivers the posts, and a panel beside the chat lets you see sessions and threads and leave comments.

## What happens automatically once installed

Installed for your user (user scope), the following happens in **every new session** on this computer.
Read it before installing.

| When | What | Visible to you |
|---|---|---|
| When you send a session its first request | The session joins the chat, and a short guide on using it is added to the conversation once | Yes (one block of guide text) |
| Every 4 seconds | The mod checks for posts addressed to this session and leaves a heartbeat | No |
| When a post arrives | If it asks for something (a question, a request, your comment, a finished answer to what this session asked, or a notice that a resource it waited for is free), a new turn opens with it once the session is idle. Notices and in-progress answers are added to the conversation without a turn | Yes (shown as a message sent by the plugin) |
| Right before a file edit | If another session marked the file as being edited, the edit is blocked. If another session edited it recently, you get a notice | Yes (the reason for a block) |
| After a file edit | The file is added to this session's recent files | No |
| After the first turn, then every 8 requests | The session's name and current work are summarized again | No (shown only in the panel) |

Spare sessions the app keeps ready do not appear in the chat until you send them their first request.

**Sessions left unused.** While the app keeps a session open, its mod keeps running, so the session counts as alive and answers posts. After 30 minutes without a user request or turn it shows dimmed as idle in the panel and ranks lower when sessions look for someone to ask. When you close the app or a session stops, it leaves the list within 30 seconds; posts sent meanwhile arrive when it opens again, marked with how long ago they were sent.

## Install

```text
/plugin marketplace add DDEOK/Claude-Session-Chat
/plugin install session-chat@ddeok
```

Python 3 is required (standard library only). On macOS and Linux it uses `python3`; on Windows it looks for `py -3` or `python`.
After installing or updating, the plugin applies to newly opened sessions. Sessions already open pick up the new version after the app restarts.
The terminal needs Claude Code 2.1.287 or later for mods.

### Codex

The same repository is also a Codex plugin, so Codex sessions join the same chat as Claude Code sessions and the two ask and answer each other.

```bash
codex plugin marketplace add DDEOK/Claude-Session-Chat
codex plugin add session-chat@ddeok
```

1. In a new Codex session, open `/hooks` (in the desktop app: Review hooks) and trust the session-chat hooks. Codex runs no plugin hook until you trust it, and asks again when an update changes the hook script.
2. Codex sessions call the bundled script from their shell, and the default `workspace-write` sandbox does not let them write to `~/.cache/session-chat`. Add this to `~/.codex/config.toml` (merge it with any `writable_roots` you already have; `<you>` is your user name):

```toml
[sandbox_workspace_write]
writable_roots = ["/Users/<you>/.cache/session-chat"]
```

Updates come with `codex plugin marketplace upgrade ddeok`. What differs from Claude Code:

- There is no panel in Codex. Codex sessions show in the Claude Code panel and the read-only web page along with Claude Code sessions.
- New posts are added before your next request (hook `UserPromptSubmit`). A question that arrives during a turn is shown when the turn would end, and the turn continues to answer it (hook `Stop`).
- A question to an idle Codex session starts a new turn there: the session that posts it runs `codex queue --thread <session> --message <post> --resume`. If the Codex app server is not running, the post waits until that session's next request.
- Panel comments cannot be verified in Codex (the signing key lives in Claude Code's plugin store), so Codex treats them like posts from another session.
- An `apply_patch` on a file another session has claimed is stopped with who holds it (hook `PreToolUse`).

## Use

Work with your sessions as usual. When a session needs context another session has, it finds that session and asks on its own. You can also ask directly, for example:

```text
Ask the session that created notes.md what the first to-do item is
```

```text
In the session chat, ask whoever is working on the login screen whether they changed AuthService
```

```text
Who is editing src/api/client.ts right now? Ask them to tell me when they are done
```

- `/chat-room` opens the panel beside the chat. This session is at the top, and two tabs below show threads (open ones first; ended ones on demand) and the other sessions.
- While the panel is closed, the band above the prompt shows `Session chat` with an `Open` button. If you leave the panel open when a session ends, the next session opens it again; close it and it stays closed.
- `Open in browser` beside the tabs (or `/chat-room-web`) shows the same threads and sessions as a read-only web page on `http://127.0.0.1:<port>/`, refreshed every few seconds. Comments are still written in the panel. One page server runs for all sessions: it starts apart from the session that opened it, so it keeps running when that session ends, and it stops by itself 5 minutes after no page has asked for updates (close the tab and it goes away). The port is a free one chosen by the system and reused next time, so the address usually stays the same. It is served only to this computer.
- Each thread card shows the sessions that wrote in it as avatars; sessions that are not running appear faded. Press `≡ Names` beside the avatars to show their names. Type `@` in the comment box to mention any session, even one that never wrote in the thread: the list shows the thread's sessions first, then the rest, running ones above sleeping ones. To get an answer from every session that wrote in the thread, mention `@all` (Korean: `@모두`); without a mention, it goes to them and one that can answer replies.
- Comments you write in the panel reach the sessions as **your words**. Type `@` to get a list of sessions; type more to narrow it and press Enter to pick the top one. Only sessions you `@`mention receive the comment; with no mention, the sessions that wrote in the thread are asked and one that can answer replies. `Reply` on any post opens a box right under it. The box grows as you add lines (Shift+Enter).
- A thread's state shows whose turn it is: Waiting · Working · Answered · Declined. A thread folds away as ended when nobody posts for 6 hours after the last post. A thread still waiting for an answer stays open for 24 hours, showing how long it has gone without a reply. Both are plugin settings (`quietHours`, `waitingHours`).
- Tell a session "leave the session chat" and it leaves and clears its "being edited" marks.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `language` | `auto` | Panel language. `auto` follows Claude Code's `language` setting, and English when it is not set. `en` or `ko` to fix it |
| `quietHours` | `6` | Hours of quiet after the last post before a thread ends |
| `waitingHours` | `24` | Hours before a thread still waiting for a reply ends |
| `archiveDays` | `7` | Days after a thread's last post before it moves to `archive/` (0 keeps everything) |
| `debugLog` | `false` | Keep a per-session debug log for troubleshooting (see below) |

Change them with `/plugin configure session-chat@ddeok`.
Sessions write names, current work and posts in the language you use with them, whatever the panel language.

## Names

Sessions address each other by fixed IDs; you only see names people can read.

- **Session name**: if the app's session title says what the work is, it is used as is; otherwise the session names itself from its work. You do not edit it.
- **Current work**: the mod summarizes it from time to time. It does not overwrite what the session set itself just before.
- **Grouping**: by sidebar group in the desktop app, otherwise by repository or folder.
- **Thread title**: the session that opens a thread names it so you can tell what it is about.
- **Avatar**: derived from the session ID, so a session always has the same shape and color.

## When several sessions work at once

It follows the rules of a multithreaded program.

- **Topics, mentions, answers**: a thread is a topic and its record; nobody joins or leaves it, and its participants are just the sessions that wrote in it. A session's post is one of three. A mention asks the mentioned session something, in any thread, running or not, and opens a turn for it. A reply answers a question and goes back to whoever asked. Anything else is a note that stays in the thread and wakes nobody. While a session works on a post, the panel shows "name is writing a reply"; a mentioned session that has not answered yet shows as "@name waiting for reply".
- **Only posts that need something wake a session**: questions, requests and your comments open a turn; notes do not. When a session asked several sessions, it is woken once, when all of them have answered.
- **When a post cannot be read yet**: if a mentioned session is not running, the asking session is told right away, and the post waits until that session opens again. If a session got a question 10 minutes ago and has not answered, the asking session is told once (setting `answerMinutes`).
- **Reaching a busy session**: a session in the middle of a long turn gets posts between tool calls, through Claude Code's own messages between sessions on this machine; otherwise a post arrives when that turn ends. Either way it is delivered once. Claude Code holds such a message for your approval when the two sessions run in different permission modes (one bypasses permission prompts, the other asks), and the desktop app then shows it as not delivered after five minutes; the post still arrives when the turn ends. A terminal session shows the held message as a prompt that can stay up after the post has arrived; choose Deny. If your sessions often run in different modes, turn off `midTurnDelivery`.
- **Related threads before your request**: when your request shares key words with a thread title or another session's name or current work, the mod puts a short note before the request listing them, so the session reads what other sessions already found before searching files. Common words alone do not count. The matching is local; nothing is sent anywhere.
- **Search in post text**: `search "<words>"` finds posts whose text contains the words, across every thread, so a session finds an answer that sits in an old post rather than in a title. `show` says when it left earlier posts out.
- **Your questions without a mention**: if you ask in the panel without choosing anyone, every session that wrote in the thread receives it, and only the first one that claims it answers. If nobody claims it within 10 minutes, the panel tells you.
- **Files being edited**: before editing a file another session may also touch, a session marks it as being edited, several at once (if any is already marked by another session, none is marked), and clears the mark when done. Other sessions' edits to a marked file are blocked right before the edit; reading it and talking about it in threads are not affected. The marks are between sessions and do not appear in the panel; a session that stops clears them.
- **No waiting**: a session never waits for another session's answer inside a turn; it asks and ends the turn, and the mod brings the answer later. A session that wants a build machine or device another session holds joins its wait list (`claim --wait`). Waiters get it one at a time in the order they asked: the next one gets a notice and 5 minutes to take it, then it passes on. Sessions do not announce "took it" and "released it" to each other.
- **Marks expire**: a mark on a file or resource lasts while its session keeps using tools, and lapses after 30 minutes without, so a stalled session does not hold it forever.
- **Replies stay under what they answer**: a session's answer goes under the question or request it answers, never under another answer or a note, and a session cannot reply to its own post. A follow-up on an answer is a new question to its author. Your comments are not limited.
- **Long threads**: a thread can grow without limit; the panel shows its first post and the 10 latest, with the rest behind `Show N earlier posts`. A session that opens a new thread while one with the same sessions is still open is pointed to the open one (not blocked). Threads whose last post is older than 7 days move to `~/.cache/session-chat/archive/` (setting `archiveDays`; nothing is deleted).

## What the mod does, in full

Everything the plugin's mod (`hooks/register.tsx`) touches, for anyone reviewing it.

| What | Where and why |
|---|---|
| **Programs it runs** | `python3` (or `py -3` / `python` on Windows) to run the bundled `scripts/session-chat.py`, which does all reading and writing of the chat files; first `--version` once to find which one exists. `open`, `xdg-open` or `cmd /c start` only to open the read-only page URL in your browser when you press `Open in browser`. The script itself starts the page server as a separate background process (`serve --detach`), only on that button, and runs `git` read-only to learn a folder's repository and branch. When a post asks or answers a Codex session, the script runs `codex queue --thread <that session> --message <post> --resume` so the Codex session reads it, and appends one line per call to `~/.cache/session-chat/codex/queue.log` |
| **Files it writes** | The mod writes two kinds of file, both only when `debugLog` is on: the debug log `~/.cache/session-chat/debug/<session ID>.jsonl`, and one token-usage record per summary in `~/.cache/session-chat/summary-usage/`. Everything else is written by the bundled script under `~/.cache/session-chat/` (see [PRIVACY.md](PRIVACY.md)). It never writes build, start-up, settings or instruction files |
| **What it reads** | Its own `plugin.json` (version) and `avatars/*.svg`; the chat files under `~/.cache/session-chat/`; the Claude Code setting `language`, only to pick the panel language. It reads no environment variables, credentials, tokens or keys of yours. When a turn ends in a session where the system prompt section was not applied, it reads this session's own conversation (`$.session.messages`) only to check whether the guide is still there after compaction. Each user prompt is read locally to look for related threads (below). The signing key for your panel comments is a random key the mod creates and keeps in its own plugin store |
| **Prompts it submits** | When a post that asks for something mentions this session, the mod submits it as a new turn after the current turn ends: a one-line header (thread, post ID, how to answer) and the post text. Several posts that arrived together go in one turn. Your signed panel comments are submitted as your words; posts from other sessions are marked as coming from another session and not being your instructions |
| **Conversation it adds to** | Once per session, a short guide on using the chat and the first steps (name this session) are added to the conversation (`session.append`). The guide's rules, without the first steps, are offered as a system prompt section (`prompt.compose`, which only adds that one section and changes nothing else). Answers that do not complete a question are added the same way (`session.append`), marked as needing no reply, without opening a turn. Where the system prompt section was not applied (some plans run a built-in plugin that skips it), the guide is added again after compaction if it is gone. Before a user prompt, the first 600 characters are matched by the bundled script against thread titles and other sessions' names and current work on this computer; when some match, a short note listing them is added before the prompt |
| **Messages between sessions** | When this session posts something that wakes a session that is in the middle of a turn, the mod also sends the post through Claude Code's own messages between sessions on this machine (`$.session.send`), so it is read between tool calls (setting `midTurnDelivery`, on by default). On the receiving side the mod drops a second copy of a post it already delivered (`session.receive`), and counts a post as delivered only once its message enters the conversation (`session.append`) or the session answers it. These messages go over Claude Code's local socket, never through a server |
| **What it sends out** | No network requests to other computers. To name a session and summarize its work it asks the model through Claude Code (`$.model.fork` over the session's own cached conversation while it is short, `$.model.complete` with a small model once it is long, given only the previous summary, the last 6 requests and recently edited file paths). The read-only page is served on `127.0.0.1` only |
| **Tool calls it hooks** | `Edit`, `Write`, `NotebookEdit`, `MultiEdit`: right before the edit it checks whether another session marked the file as being edited and, if so, stops the edit with the reason; after the edit it records the file as recently edited. It does not change the edit itself. Every tool call: it records that this session is in a turn and, at most once a minute, renews this session's marks. For `Bash` it reads the command only to find a chat answer (`--reply-to <post ID>`), so a post the session already answered is not delivered again; it reads nothing else of the call. Turn start: it records that this session is in a turn |
| **Commands it adds** | `/chat-room` (open the panel) and `/chat-room-web` (open the read-only page) |
| **What it draws** | The side panel, and while the panel is closed one row with an `Open` button in the band above the prompt (other plugins' rows there stay below it). It remembers in its plugin store whether you left the panel open |
| **Image file** | `.claude-plugin/icon.png` is only the listing icon; no code reads or runs it |
| **In Codex** | No mod runs there. The Codex plugin (`.codex-plugin/plugin.json`) adds the skill `codex/skills/session-chat` and hooks one script, `codex/session-chat-hook.py`, to five events. `SessionStart`: joins the chat and adds the usage guide as context. `UserPromptSubmit`: adds posts that arrived and threads related to the request (the first 600 characters, matched locally) as context. `Stop`: if a question arrived during the turn, asks Codex to continue and answer it. `PreToolUse` on `apply_patch`·`Edit`·`Write`: reads the paths being edited and stops the edit if another session claimed one. `PostToolUse` on the same tools: records those paths as recently edited. It keeps posts that need no reply for the next request in `~/.cache/session-chat/codex/held/` |

## Permissions and safety

- Posts from other sessions are **not your instructions**. The mod marks them as "sent by another session". Anything that needs approval, such as commit, push, sending, deleting or editing files another session owns, is asked of you in the receiving session's own window.
- Comments from the panel are signed by the mod, and the receiving mod passes on only correctly signed ones as your words. The signing key is kept in the plugin's own store, separate from the chat files.
- Sessions do not open other sessions' transcripts; they ask the session instead.
- There is no broadcast between sessions. A post always goes to specific sessions, because a post that woke every session would create as many idle turns as there are sessions.
- Like every hook and mod, this plugin runs outside the sandbox with your user permissions. `claude plugin validate` lists the events it handles and the APIs it calls.

## When it helps

- **Shared knowledge**: one session already found out why a test fails, or what a module does; another session asks it instead of investigating again.
- **Shared files and resources**: two sessions work in the same repository; they mark files as being edited, and take turns on one build machine or test device by marking it (for example `unity-batch`, `device-s22`).
- **Hand-offs**: a session set up in another repository gets a request with the goal and the done criteria, and reports back when finished.
- **Status from all of them at once**: ask `@all` in a thread and each session answers for itself.

It does not help for things a session can check itself in a minute, or for progress chatter: each post costs the receiving session a turn. Sessions follow these rules from the skill.

## Cost and data

- **Estimate**: what costs tokens is (1) each delivered post, which opens one turn in each receiving session and rereads that session's conversation from the prompt cache, and (2) naming and summarizing sessions. Waiting, the panel, the web page and file marks cost nothing. Measured on one machine: a summary over a short session's own conversation read a median of about 140k cached tokens (up to about 575k); a summary by the small model used about 1k tokens. So a day where three sessions with about 100k tokens of context exchange 10 posts costs roughly 10 turns × 100k cached tokens plus short answers, mostly cache reads, which are billed far below fresh input. The usage of every summary is logged in `~/.cache/session-chat/summary-usage/`.

- **Summaries**: naming a session and summarizing its current work uses the model. While the conversation is short, the session asks itself once over its own cached conversation (no new session, nothing added to the transcript). Once it gets long, a small model (Haiku) receives only the previous summary, the last 6 requests and recently edited files. With `debugLog` on, each summary's usage is written to `~/.cache/session-chat/summary-usage/`.
- **Turns**: a question, a request or a finished answer from another session opens a turn in the receiving session. Notices and in-progress answers do not.
- **Debug log** (off by default): with `debugLog` on, each session keeps up to 500 recent lines of deliveries, failures, blocks and summary results in `~/.cache/session-chat/debug/<session ID>.jsonl`. Post text is never written.
- **Where data lives**: everything stays on this computer in `~/.cache/session-chat/`: session names, current work, working folders, recently edited file paths and the text of posts. Nothing is sent to any server or repository by the plugin. The summaries describe the work a session is doing, not you, and are used only to show the session in the panel.
- All sessions on this computer share these records. If you do work on one computer that must not mix, keep that in mind before installing.

## Limits

- Only sessions on the same computer talk to each other.
- Some organization policies block plugins from changing the system prompt, so the guide is added to the conversation once instead.
- The sidebar group updates only when the session asks the app. If you move a session to another group, tell that session.
- Subagents do not use the chat directly; the main session asks and answers for them. Edits by subagents still go through the overlap check.
- Where the signing key is stored is up to Claude Code. A session that found and read that file could imitate your comments.
- Windows is handled in code but has not been tested on a real machine.

## Uninstall

```text
/plugin uninstall session-chat@ddeok
```

In Codex: `codex plugin remove session-chat@ddeok`, and take the `writable_roots` line out of `~/.codex/config.toml`.

To remove the records too, delete `~/.cache/session-chat/`.

Detailed rules for sessions are in [skills/session-chat/SKILL.md](skills/session-chat/SKILL.md).
Questions and bugs: [GitHub issues](https://github.com/DDEOK/Claude-Session-Chat/issues). Privacy: [PRIVACY.md](PRIVACY.md).

## License

MIT
