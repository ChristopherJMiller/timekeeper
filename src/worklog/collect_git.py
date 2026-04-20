"""Git evidence collection: commits in window + WIP at stop time.

Uses subprocess directly — no GitPython. The per-commit patch is inlined
only when the commit is small (≤200 lines changed) so a single large
refactor can't blow past the context window alone.
"""
from __future__ import annotations

import datetime as dt
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


COMMIT_SEP = "===COMMIT==="
FILES_SEP = "---FILES---"
PRETTY = f"%n{COMMIT_SEP}%n%H%n%an <%ae>%n%aI%n%s%n%b%n{FILES_SEP}"

PATCH_LINE_CAP = 200       # commits larger than this skip the patch
PER_COMMIT_PATCH_CAP = 8000  # byte cap per inlined patch
WIP_DIFF_CAP = 6000


@dataclass
class Commit:
    repo: str
    sha: str
    author: str
    date: str
    subject: str
    body: str
    stat: str
    lines_changed: int
    patch: str = ""


@dataclass
class Wip:
    repo: str
    branch: str
    status: str
    diffstat: str
    diff: str


@dataclass
class GitEvidence:
    commits: list[Commit] = field(default_factory=list)
    wip: list[Wip] = field(default_factory=list)


def _run(args: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _sum_stat_total(stat_block: str) -> int:
    for line in reversed(stat_block.splitlines()):
        if "changed" in line:
            nums = [int(s) for s in line.replace(",", " ").split() if s.isdigit()]
            return sum(nums[1:]) if len(nums) >= 2 else 0
    return 0


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def commits_in_window(
    repo: Path,
    author: str,
    since: dt.datetime,
    until: dt.datetime,
) -> list[Commit]:
    raw = _run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            f"--author={author}",
            f"--since={since.isoformat()}",
            f"--until={until.isoformat()}",
            "--all",
            "--no-merges",
            f"--pretty=format:{PRETTY}",
            "--stat=200,160",
            "--stat-count=20",
        ],
        cwd=repo,
    )
    commits: list[Commit] = []
    for chunk in raw.split(COMMIT_SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, stat = chunk.partition(FILES_SEP)
        lines = head.strip().splitlines()
        if len(lines) < 4:
            continue
        sha, author_str, date_str, subject, *body_lines = lines
        stat_str = stat.strip()
        total = _sum_stat_total(stat_str)
        commit = Commit(
            repo=repo.name,
            sha=sha.strip(),
            author=author_str.strip(),
            date=date_str.strip(),
            subject=subject.strip(),
            body="\n".join(body_lines).strip(),
            stat=stat_str,
            lines_changed=total,
        )
        if total <= PATCH_LINE_CAP:
            patch = _run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "show",
                    "--no-color",
                    "--pretty=format:",
                    "-U1",
                    commit.sha,
                ],
                cwd=repo,
            )
            commit.patch = patch[:PER_COMMIT_PATCH_CAP]
        commits.append(commit)
    return commits


def wip_snapshot(repo: Path) -> Wip | None:
    status = _run(["git", "-C", str(repo), "status", "--porcelain"], cwd=repo)
    if not status.strip():
        return None
    branch = _run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo
    ).strip()
    diffstat = _run(["git", "-C", str(repo), "diff", "HEAD", "--stat"], cwd=repo)
    diff = _run(["git", "-C", str(repo), "diff", "HEAD"], cwd=repo)[:WIP_DIFF_CAP]
    return Wip(
        repo=repo.name,
        branch=branch,
        status=status,
        diffstat=diffstat,
        diff=diff,
    )


def collect(
    repos: list[Path],
    author: str,
    since: dt.datetime,
    until: dt.datetime,
) -> GitEvidence:
    ev = GitEvidence()
    for repo in repos:
        if not is_git_repo(repo):
            continue
        ev.commits.extend(commits_in_window(repo, author, since, until))
        snap = wip_snapshot(repo)
        if snap:
            ev.wip.append(snap)
    return ev
