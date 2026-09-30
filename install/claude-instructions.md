# Hardware remote (esp32_remote)

A physical LILYGO T-Display S3 remote (LCD + 8 buttons + rotary encoder) may
be connected through the `esp32_remote` MCP server and the claude-hardware-remote
hooks. When it is connected (the `remote_status` tool says CONNECTED, or a
hook context line says the remote is connected):

- Use the `mcp__esp32_remote__ask_user` tool whenever you need a decision or
  clarification from the human: yes/no confirmations, choosing between
  approaches, selecting an option, or any point where you would otherwise stop
  and ask in the chat. Do not ask important questions only as plain assistant
  text while the remote is connected.
- Prefer structured choices: `yes_no=true` for confirmations, `options=[...]`
  (up to 8 short labels, more are paginated) when the answers are enumerable,
  and `allow_free_text=true` only when arbitrary input is genuinely needed.
  Keep the question under ~200 characters; the screen is 320x170 pixels.
- If `ask_user` reports the remote is unavailable, disconnected, cancelled, or
  timed out, fall back immediately to a normal conversational question or the
  built-in AskUserQuestion tool. Do not retry `ask_user` in a loop and do not
  block on it.
- Permission prompts and stop/continue prompts are handled by hooks; you do not
  need to do anything special for them.

This instruction only changes *how* a question you would ask anyway reaches
the human. It never overrides Claude Code's permission system, safety rules,
or tool-use policies.
