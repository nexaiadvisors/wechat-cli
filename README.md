# wechat-cli

Let Claude Code (or any MCP-capable agent) read and send messages in **WeChat for Mac** — by looking at the screen the way a person does.

WeChat 4.x hides its chat window from the macOS Accessibility API, so tools that read the accessibility tree find nothing. This tool takes a different route:

- **Reads** the WeChat window with Apple's built-in text recognition (Vision OCR, Chinese and English).
- **Acts** with ordinary mouse clicks, keyboard shortcuts and the clipboard.
- **Uses accessibility only for layout** — WeChat still reports where its chat list and input box are, just not what is in them.

Nothing is decrypted, injected or patched. WeChat's own app and security settings stay untouched.

## What it can do

| Action | CLI | MCP tool |
|---|---|---|
| Is WeChat running and logged in? | `wechat status` | `wechat_status` |
| Recent chats with time, last message and unread count | `wechat list [--pages N]` | `wechat_list_chats` |
| Search contacts and group chats | `wechat search QUERY` | `wechat_search` |
| Read a chat, scrolling back N extra screens; group messages carry the sender | `wechat read CHAT [--pages N] [--from-list]` | `wechat_read_chat` |
| Send text | `wechat send CHAT --text "..." --yes` | `wechat_prepare_send` + `wechat_send_message` |
| Send a photo, video or file | `wechat send CHAT --attach PATH --yes` | same, with `file_path` |

Listing and searching open no chat, so nothing is marked as read. Reading a chat opens it, which marks it read.

Not supported: voice/video calls, payments and red packets, Moments, adding contacts.

## Every send needs your approval

Sending is the risky part, so the tool makes it hard to do by accident:

1. **Preview, then your OK.** `wechat_prepare_send` returns the exact chat and content plus a one-time token (valid 10 minutes). The agent is told to show this to you word for word and send only after you approve that exact content in the conversation. The token only works for the same chat and the same text or file.
2. **Optional second approval on the Mac.** Set `WECHAT_CONFIRM_DIALOG=1` and every send also pops a native macOS dialog showing the chat and exact text (or file name, size and source path). Nothing goes out unless a person clicks Send there, so even an agent that ignores instructions, or text hidden in a message it read, cannot send on its own. It is off by default because a pop-up per message gets tedious; turn it on if the agent runs unattended or reads untrusted content.
3. **Exact recipient.** A chat opens only when exactly one contact or group matches the name exactly (case-sensitive). Zero or several different matches → refused, with the candidates listed. (The same chat appearing under both "Most used" and "Contacts" counts as one.)
4. **Checks before and after.** The chat title is verified by OCR before typing and again right before pressing Send; the pasted text must be visible in the input box; afterwards the new bubble is checked on screen.
5. **Content limits.** Text over 1,200 characters or 25 lines, consecutive blank lines, or hidden/control Unicode characters are refused. Files over 100 MB and symlinks are refused; a private read-only copy of the file is what gets sent, so it cannot change in between.
6. **Hands off.** It waits until nobody has touched the keyboard or mouse for 3 seconds, stops if WeChat loses focus before typing, and restores your clipboard afterwards.

To turn the dialog on for Claude Code:

```bash
claude mcp add --scope user wechat -e WECHAT_CONFIRM_DIALOG=1 -- uv --directory "$(pwd)" run wechat-mcp-local
```

## Requirements

- macOS 14 or later (developed on macOS 27), Apple Silicon or Intel
- WeChat for Mac 4.x, logged in, window not minimized (English or Chinese UI)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- macOS permissions for the app that launches the tool (Terminal, iTerm, Ghostty, the Claude app, …):
  - **Privacy & Security → Accessibility** (to click and type)
  - **Privacy & Security → Screen Recording** (to read the window)

## Install

```bash
git clone https://github.com/nexaiadvisors/wechat-cli.git
cd wechat-cli
uv sync
uv run wechat status
```

### Add it to Claude Code

```bash
claude mcp add --scope user wechat -- uv --directory "$(pwd)" run wechat-mcp-local
```

Any other MCP client can launch the same command over stdio: `uv --directory /path/to/wechat-cli run wechat-mcp-local`.

## Usage

```bash
uv run wechat list                      # recent chats; (3) = 3 unread, (•) = red dot (e.g. muted)
uv run wechat search "Alice"
uv run wechat read "Alice" --pages 3    # current screen + 3 screens of history
uv run wechat send "Alice" --text "Running 10 minutes late" --yes
uv run wechat send "Project Team" --attach ~/Desktop/plan.pdf --yes
```

Chats are found with WeChat's search. If search can't find a name (one-character names are hard for OCR in the search results), the tool falls back to the chat list; `--from-list` (MCP: `from_list=true`) goes straight to the chat list. Either way the name must match exactly, and the opened chat is verified by its title, or by the highlighted chat-list row when the title is too short to read.

`send` without `--yes` only prints what it would send. With `WECHAT_CONFIRM_DIALOG=1` set, `--yes` is followed by the macOS dialog.

## How it behaves on your Mac

- It **moves and resizes the WeChat window** (to 1100×820 at the top left) and widens the chat list, so the layout is predictable.
- Each action **takes over the screen for a few seconds**: WeChat comes to the front and the mouse moves. Don't type during that time; the tool waits for you to stop first.
- Only one agent should drive WeChat at a time.

## Limitations

- **OCR is not perfect.** Expect the occasional misread name or character. Recipient matching is exact, so a misread name fails safely rather than picking someone else.
- Stickers, photos and videos have no text, so reading shows only what text is around them.
- Group sender names and the "me/them" split are inferred from layout and can be wrong on unusual messages (cards, mini programs, quotes).
- Layout assumptions are based on WeChat 4.1.x; a WeChat redesign can break them.

## WeChat's terms

WeChat's terms of service and acceptable-use rules restrict third-party tools and automated use of accounts. Using this tool may lead to warnings or restrictions on your account. It is meant for light, personal, human-supervised use of **your own** account — never for bulk messaging, marketing, spam or unattended bots. You are responsible for how you use it. This project is not affiliated with or endorsed by Tencent.

## License

MIT — see [LICENSE](LICENSE).
