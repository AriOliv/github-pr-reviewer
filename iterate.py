import json
import os
import sys
import urllib.error
import urllib.request


def has_write_permission(repo: str, username: str, token: str) -> bool:
    """Verify whether the comment author has write or admin access to the repository."""
    if not repo or not username or not token:
        return False

    url = f"https://api.github.com/repos/{repo}/collaborators/{username}/permission"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GitHub-Action-Refine",
        },
    )

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            permission = data.get("permission", "")
            return permission in ("admin", "write")
    except urllib.error.HTTPError as e:
        print(f"HTTP error checking permissions for {username}: {e.code} {e.reason}")
        return False
    except Exception as e:
        print(f"Failed to check permissions for {username}: {e}")
        return False


def refine():
    """Process /refine commands on open bot pull requests (auto-fix/*, security-fix/*)."""
    comment_author = os.environ.get("COMMENT_AUTHOR")
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    if not comment_author:
        print("No COMMENT_AUTHOR provided. Skipping refine.")
        return

    if not repo or not token:
        print("Missing GITHUB_REPOSITORY or GITHUB_TOKEN environment variables.")
        return

    # Authorization check for /refine command (Finding id: 4ac664eb05cf7105)
    if not has_write_permission(repo, comment_author, token):
        print(f"Unauthorized: User '{comment_author}' lacks write permissions on '{repo}'. /refine command aborted.")
        sys.exit(1)

    print(f"User '{comment_author}' is authorized. Executing /refine workflow...")


if __name__ == "__main__":
    refine()
