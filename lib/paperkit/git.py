"""The git repository the paper is in: its commit, uncommitted changes, and logs not yet pushed."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Git in `cwd`, without the GIT_* variables a hook or `git rebase --exec` exports: with GIT_DIR set,
    git would read the repo it names instead of the one holding `cwd`."""
    env = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False
    )


@dataclass(frozen=True)
class Repo:
    root: Path

    @classmethod
    def of(cls, path: Path) -> "Repo | None":
        """The repository holding `path`, or None outside one."""
        found = run(["rev-parse", "--show-toplevel"], path)
        return cls(Path(found.stdout.strip())) if found.returncode == 0 else None

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(list(args), self.root)

    def head(self) -> str:
        """The commit HEAD names, or "" with none yet (git echoes the unresolved "HEAD" then)."""
        found = self.git("rev-parse", "HEAD")
        return found.stdout.strip() if found.returncode == 0 else ""

    def dirty(self) -> list[str]:
        """The paths with uncommitted changes, untracked ones included."""
        return [
            line[3:] for line in self.git("status", "--porcelain").stdout.splitlines()
        ]

    def relative(self, path: str) -> str:
        """`path` from the repo's root, when it is inside the repo."""
        return (
            str(Path(path).relative_to(self.root))
            if Path(path).is_relative_to(self.root)
            else path
        )

    def unpushed(self, paths: list[str], upstream: str) -> list[str]:
        """The paths (relative to the root) that aren't tracked, or have commits not on `upstream`."""
        if self.git("rev-parse", "--verify", "--quiet", upstream).returncode != 0:
            return paths
        return [
            path
            for path in paths
            if self.git("ls-files", "--error-unmatch", "--", path).returncode != 0
            or self.git(
                "log", "--format=%h", f"{upstream}..HEAD", "--", path
            ).stdout.strip()
        ]

    def is_main_worktree(self) -> bool:
        """Whether this is the repository's main worktree, not one `git worktree add` made: its git dir
        is the common one."""
        found = self.git("rev-parse", "--absolute-git-dir", "--git-common-dir")
        git_dir, common = found.stdout.splitlines()[:2]
        return Path(git_dir).resolve() == (self.root / common).resolve()
