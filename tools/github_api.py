#!/usr/bin/env python3
"""
github_api.py — direct GitHub API helper for The AI Journal.

Replaces the browser/CodeMirror publish flow (screenshots, synthetic paste
events, select-all-then-delete-then-paste) with plain HTTPS calls to the
GitHub Contents and Issues APIs. No browser session required.

Auth: reads a token from the GH_TOKEN environment variable. Never hardcode
a token in this file or any file committed to the repo. The token is not
logged, printed, or written to disk by anything in this module.

Required scope: a fine-grained PAT scoped to colm18-gif/theaijournal only,
with Contents: read/write and Issues: read/write. Nothing else.

Usage as a library:

    from github_api import GitHubRepo
    repo = GitHubRepo("colm18-gif", "theaijournal")
    content, sha = repo.get_file("issues.json")
    repo.put_file("state.json", new_content, "Update state.json after publish", sha=old_sha)
    repo.comment_and_close(21, "Declined - off-format...")

Usage from the command line (mainly for quick checks):

    GH_TOKEN=... python3 github_api.py get issues.json
    GH_TOKEN=... python3 github_api.py issues --state open
"""

import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error


class GitHubAPIError(RuntimeError):
    pass


class GitHubRepo:
    def __init__(self, owner: str, repo: str, token: str | None = None):
        self.owner = owner
        self.repo = repo
        self.token = token or os.environ.get("GH_TOKEN")
        if not self.token:
            raise GitHubAPIError(
                "No token found. Set GH_TOKEN in the environment before using this module."
            )
        self.base = f"https://api.github.com/repos/{owner}/{repo}"

    # ---- low-level request helper ----

    def _request(self, method: str, url: str, body: dict | None = None, retries: int = 3):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        last_err = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    parsed = json.loads(raw)
                    msg = parsed.get("message", raw.decode("utf-8", "replace"))
                except Exception:
                    msg = raw.decode("utf-8", "replace")
                # Retry on secondary rate limit / abuse detection, not on 4xx logic errors
                if e.code in (403, 429) and "rate limit" in msg.lower() and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    last_err = GitHubAPIError(f"{e.code}: {msg}")
                    continue
                raise GitHubAPIError(f"{method} {url} -> {e.code}: {msg}") from e
            except urllib.error.URLError as e:
                last_err = GitHubAPIError(f"{method} {url} -> network error: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_err
        raise last_err or GitHubAPIError("request failed with no captured error")

    # ---- Contents API ----

    def get_file(self, path: str, ref: str = "main") -> tuple[str, str]:
        """Return (decoded_text_content, sha). Raises GitHubAPIError if the file doesn't exist."""
        url = f"{self.base}/contents/{path}?ref={ref}"
        result = self._request("GET", url)
        if isinstance(result, list):
            raise GitHubAPIError(f"{path} is a directory, not a file")
        content = base64.b64decode(result["content"]).decode("utf-8")
        return content, result["sha"]

    def file_exists(self, path: str, ref: str = "main") -> bool:
        try:
            self.get_file(path, ref=ref)
            return True
        except GitHubAPIError:
            return False

    def put_file(self, path: str, content: str, message: str, sha: str | None = None,
                 branch: str = "main") -> dict:
        """
        Create or update a file. If sha is None and the file already exists, this
        will fail with a 409/422 — pass the sha from get_file() when updating.
        For a brand-new file, leave sha as None.
        """
        url = f"{self.base}/contents/{path}"
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return self._request("PUT", url, body)

    def update_file(self, path: str, content: str, message: str, branch: str = "main") -> dict:
        """Convenience: fetches the current sha automatically, then updates. One extra API call
        versus put_file(), but avoids the caller having to track shas across a session."""
        _, sha = self.get_file(path, ref=branch)
        return self.put_file(path, content, message, sha=sha, branch=branch)

    def delete_file(self, path: str, message: str, sha: str | None = None,
                     branch: str = "main") -> dict:
        if sha is None:
            _, sha = self.get_file(path, ref=branch)
        url = f"{self.base}/contents/{path}"
        body = {"message": message, "sha": sha, "branch": branch}
        return self._request("DELETE", url, body)

    # ---- Git Data API (batched multi-file commits) ----

    def commit_files(self, files: dict[str, str], message: str, branch: str = "main") -> dict:
        """
        Write multiple files in a single commit, using the Git Data API directly
        instead of one Contents-API put_file() call per file. This is the default
        way to publish an issue: issue-NNNN.html, issues.json, feed.xml and
        sitemap.xml go in as one commit, not four.

        files: {path: content} — content is plain text (UTF-8), not base64.
               Works for both new and existing paths; no per-file sha needed.

        Steps: read the branch's current commit + tree, create one blob per file,
        build one new tree on top of the current tree, create one commit pointing
        at it, then fast-forward the branch ref. Five API calls total regardless
        of file count, versus 2 * N for get_file/put_file per file.
        """
        ref_get_url = f"{self.base}/git/ref/heads/{branch}"
        ref_update_url = f"{self.base}/git/refs/heads/{branch}"
        ref = self._request("GET", ref_get_url)
        parent_sha = ref["object"]["sha"]

        parent_commit = self._request("GET", f"{self.base}/git/commits/{parent_sha}")
        base_tree_sha = parent_commit["tree"]["sha"]

        tree_entries = []
        for path, content in files.items():
            blob = self._request("POST", f"{self.base}/git/blobs", {
                "content": content,
                "encoding": "utf-8",
            })
            tree_entries.append({
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob["sha"],
            })

        new_tree = self._request("POST", f"{self.base}/git/trees", {
            "base_tree": base_tree_sha,
            "tree": tree_entries,
        })

        new_commit = self._request("POST", f"{self.base}/git/commits", {
            "message": message,
            "tree": new_tree["sha"],
            "parents": [parent_sha],
        })

        self._request("PATCH", ref_update_url, {"sha": new_commit["sha"]})
        return new_commit

    # ---- Issues API ----

    def list_issues(self, state: str = "open", per_page: int = 100) -> list:
        url = f"{self.base}/issues?state={state}&per_page={per_page}"
        return self._request("GET", url)

    def get_issue(self, number: int) -> dict:
        url = f"{self.base}/issues/{number}"
        return self._request("GET", url)

    def comment_on_issue(self, number: int, body: str) -> dict:
        url = f"{self.base}/issues/{number}/comments"
        return self._request("POST", url, {"body": body})

    def close_issue(self, number: int, reason: str = "completed") -> dict:
        url = f"{self.base}/issues/{number}"
        return self._request("PATCH", url, {"state": "closed", "state_reason": reason})

    def comment_and_close(self, number: int, body: str, reason: str = "completed") -> dict:
        """The common case: post a comment, then close. Matches the two-step
        'type comment, click Close with comment' flow from the browser, but as
        two direct calls with no risk of a misplaced click."""
        self.comment_on_issue(number, body)
        return self.close_issue(number, reason=reason)


# ---- minimal CLI for quick manual checks ----

def _main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    repo = GitHubRepo("colm18-gif", "theaijournal")
    cmd = sys.argv[1]

    if cmd == "get" and len(sys.argv) >= 3:
        content, sha = repo.get_file(sys.argv[2])
        print(f"--- sha: {sha} ---")
        print(content)
    elif cmd == "issues":
        state = "open"
        if "--state" in sys.argv:
            state = sys.argv[sys.argv.index("--state") + 1]
        issues = repo.list_issues(state=state)
        for i in issues:
            print(i["number"], "|", i["title"])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    _main()
