# The AI Journal — publishing mechanics

Reference this file only when the publish step in SKILL.md §6 fails or behaves unexpectedly. It has no editorial content — everything here is "how", not "what" or "why". If you're reading this before you've built the article, you're reading it too early.

## Fast path: direct API via tools/github_api.py (preferred when available)

If a `GH_TOKEN` environment variable is set this session, use `tools/github_api.py` instead of the browser. It wraps the GitHub Contents and Issues APIs directly — no browser, no screenshots, no CodeMirror, no paste events, and none of the failure modes documented below (which are specific to the browser flow).

```python
import sys
sys.path.insert(0, 'tools')  # or wherever the repo's tools/ dir was fetched to locally
from github_api import GitHubRepo

repo = GitHubRepo("colm18-gif", "theaijournal")

# Read a file (returns content + sha; sha is needed to update it)
content, sha = repo.get_file("issues.json")

# Update an existing file
repo.update_file("state.json", new_content, "Update state.json after publish")
# — or, if you already have the sha from a get_file() call in this session:
repo.put_file("state.json", new_content, "Update state.json after publish", sha=sha)

# Create a brand-new file (no sha needed)
repo.put_file("contribution-0009.html", html_content, "Publish issue 10")

# List open issues / read one
issues = repo.list_issues(state="open")
issue = repo.get_issue(21)

# Comment and close in one call — this is the accept/decline flow
repo.comment_and_close(21, "Declined - off-format to the point of needing rewriting rather than editing. ...")
```

**The token is never persisted.** `GH_TOKEN` must be present in the environment at the start of the session — it is provided fresh each time by the person running the journal (fine-grained PAT, scoped to this repo only, Contents + Issues read/write, short expiry). Do not write it to any file, do not commit it, do not put it in `state.json` or anywhere else. If `GH_TOKEN` is not set, fall back to the browser flow below and say so — don't ask the person to paste a token into chat unless they've already offered; it's their call whether that session's automation is worth generating one for.

**When the fast path still needs a human decision:** `github_api.py`'s CLI (`python3 github_api.py get <path>` / `issues --state open`) is useful for a quick read without writing any code, if you just need to check something.

Everything from here down describes the browser flow, used when no token is available.

## Committing files

There is no GitHub MCP connector. Commit through the GitHub web UI with the Chrome tools.

**New file** (one that doesn't exist in the repo yet): load `https://github.com/colm18-gif/theaijournal/new/main?filename=path%2Fto%2Ffile.ext`, click into the CodeMirror editor, and insert the body by dispatching a synthetic `paste` ClipboardEvent at `.cm-content`.

**Replacing an existing file**: load `https://github.com/colm18-gif/theaijournal/edit/main/path/to/file.ext`, click into the editor, select all (`Ctrl+A`), **delete the selection explicitly before pasting** — do not rely on paste-over-selection to replace the content. Confirm the editor is empty (screenshot or check `editor.textContent.length`), then dispatch the paste event with the full replacement content.

**Why the explicit delete matters:** a select-all-then-paste in one step has, at least once, resulted in the new content being appended after the old rather than replacing it — producing a file with the old content duplicated after a second copy of the header/footer. This is silent: the paste reports success and the visible top of the file looks correct. Always scroll or jump to the end of the file after pasting and confirm it terminates cleanly (e.g. `</html>` appears exactly once), and re-fetch the raw file from `raw.githubusercontent.com` after committing to verify entry counts match expectations, before considering the file done.

For small files (a few KB), select-all → paste → verify → commit is fine as one pass. For anything past roughly 15–20 KB of base64, prefer a **targeted edit** over a full-file replace: click to a known anchor (a marker comment, a specific line), and paste only the new fragment, leaving the rest of the document untouched. This is both cheaper and safer than round-tripping the whole file.

Uploading a file with an existing name via `/upload/main` also replaces it, and avoids the CodeMirror paste entirely — prefer it for files without special characters when the upload tool is available.

## Verifying a raw-markdown source (submission text)

Rendered HTML on a GitHub issue page normalises punctuation, so `get_page_text` output is generally reliable for curly quotes and em-dashes. If exact byte-for-byte source is needed, fetch `https://api.github.com/repos/colm18-gif/theaijournal/issues/{n}` via `fetch()` in the page's own JS context (authenticated by the browser session) rather than an unauthenticated `curl` from the sandbox — the sandbox has no GitHub token and will rate-limit quickly on anonymous requests.

## Browser quirks

- If the window is hidden or zero-sized, `innerWidth` reads 0, screenshots fail, and coordinate clicks and `execCommand` do nothing.
- Ref-based clicks on plain buttons usually work, but have occasionally landed on the wrong element (e.g. a toolbar "Quote" button instead of "Close with comment") when the layout shifted between locating the ref and clicking it. If a click's effect doesn't match what was intended, screenshot immediately and undo before proceeding — don't assume the click landed where planned.
- For React-controlled inputs, set the value with the native `HTMLInputElement.prototype.value` setter and dispatch a bubbling `input` event rather than typing character-by-character.
- CodeMirror only renders visible lines: `.cm-content.innerText` returns a **truncated** document for anything long. Never read a file's contents back out of CodeMirror and re-paste them — build the replacement content locally (or fetch it via the raw GitHub API) and paste that instead.
- The Claude-in-Chrome extension connection can drop mid-task (observed once during this journal's operation) and time out every subsequent browser tool call, including trivial ones. If several consecutive browser calls time out, stop retrying blindly — check `list_connected_browsers`, and if multiple browsers are connected, the person will need to pick one before tools work again.
- GitHub Pages takes up to a minute to deploy. A 404 immediately after committing is normal — don't treat it as a failed publish.

## Closing a GitHub issue with a comment

Click directly into the comment `<textarea>` by its screen coordinates (element-ref clicks have occasionally focused the wrong thing or silently failed to register typed text — verify with a screenshot that the typed text actually appears in the box before proceeding). Once text is confirmed present, the button label changes from "Close issue" to "Close with comment" — click that, not "Comment" alone, or the issue stays open.

## If the browser is unavailable

Leave the finished files in the outputs directory and say clearly that they are built but not yet pushed. Never report an issue as published when the commit did not go through.

## Verifying a publish actually landed

Fetching `raw.githubusercontent.com/colm18-gif/theaijournal/main/<file>` from the sandbox (via `bash_tool`/`curl`) is faster and cheaper than reloading the page in the browser, and it reflects the committed content immediately — no Pages deploy delay, since it reads straight from the git ref. Use it as the primary verification step; reserve a live-site browser check for the final end-to-end confirmation.
