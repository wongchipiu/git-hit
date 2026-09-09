"""本地 Git 存档。

决策 2：每日报告 commit 到本地仓库作为历史存档，**不 push 任何远端**。
回溯用 ``git log`` 即可，既满足可追溯，又避免把个人仓库推到公网。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Sequence

from ..logging_setup import get_logger

log = get_logger("radar.git")


def _run(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(list(args), cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="ignore")
        return p.returncode, (p.stdout or "").strip() or (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "git 不可用（未安装或不在 PATH）"


def ensure_repo(root: Path) -> bool:
    """仓库不存在时初始化（仅在用户显式 init 时调用）；并确保本地提交身份存在。"""
    code, _ = _run(["git", "rev-parse", "--is-inside-work-tree"], root)
    if code != 0:
        code, out = _run(["git", "init"], root)
        if code != 0:
            log.warning("git init 失败：%s", out)
            return False
    # 仓库可能是拷贝/克隆来的，或本机全局未配置身份；确保本地有提交身份（仅影响本仓库）
    code, name = _run(["git", "config", "user.name"], root)
    if code != 0 or not name.strip():
        _run(["git", "config", "user.name", "TreasureRadar"], root)
    code, email = _run(["git", "config", "user.email"], root)
    if code != 0 or not email.strip():
        _run(["git", "config", "user.email", "radar@localhost"], root)
    return True


def repo_has_changes(root: Path) -> bool:
    code, out = _run(["git", "status", "--porcelain"], root)
    return code == 0 and bool(out.strip())


def commit_digest(root: Path, paths: Sequence[Path], message: str, push: bool = False) -> bool:
    """把报告文件加入暂存并提交。``push`` 恒为 False 时绝不推送。"""
    code, _ = _run(["git", "rev-parse", "--is-inside-work-tree"], root)
    if code != 0:
        log.info("当前目录不是 Git 仓库，跳过存档提交")
        return False

    rel = []
    for p in paths:
        try:
            rel.append(str(Path(p).resolve().relative_to(root.resolve())))
        except ValueError:
            rel.append(str(p))
    if not rel:
        return False

    code, out = _run(["git", "add", "--", *rel], root)
    if code != 0:
        log.warning("git add 失败：%s", out)
        return False

    code, out = _run(["git", "diff", "--cached", "--quiet"], root)
    if code == 0:
        log.info("无内容变化，跳过本次提交")
        return False

    code, out = _run(["git", "commit", "-m", message], root)
    if code != 0:
        log.warning("git commit 失败：%s", out)
        return False
    log.info("已提交存档：%s", message)

    if push:  # pragma: no cover - 默认永不开启
        log.warning("检测到 push=True，但按决策 2 本产品不推送远端，已忽略")
    return True
