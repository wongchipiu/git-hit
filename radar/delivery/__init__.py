"""交付层：本地 Git 存档（决策 2：只 commit，绝不 push）。"""

from .git_archive import commit_digest, ensure_repo, repo_has_changes

__all__ = ["commit_digest", "ensure_repo", "repo_has_changes"]
