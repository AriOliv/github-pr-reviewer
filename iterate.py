import json
import os
import subprocess
import sys

def has_write_permission(repo: str, username: str) -> bool:
    if not repo or not username:
        return False
    try:
        res = subprocess.run(
            ["gh", "api", f"repos/{repo}/collaborators/{username}/permission"],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        permission = data.get("permission", "")
        return permission in ("admin", "write", "maintain")
    except Exception as e:
        print(f"Permission check failed for {username}: {e}")
        return False

def refine():
    repo = os.environ.get("GITHUB_REPOSITORY")
    comment_author = os.environ.get("COMMENT_AUTHOR")

    if not comment_author or not has_write_permission(repo, comment_author):
        print(f"Unauthorized: User '{comment_author}' does not have write permissions on '{repo}'.")
        sys.exit(1)

    print(f"Authorized /refine trigger by {comment_author}.")

if __name__ == "__main__":
    refine()
