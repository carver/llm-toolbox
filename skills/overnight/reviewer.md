# Brief for an overnight reviewer

You review one commit range in a worktree. Read only: change no file and post nothing.

Read:
- the diff: start from `git -C <worktree> diff --stat <base>..HEAD`, then read it one group of files at a time (`git -C <worktree> diff <base>..HEAD -- PATHS`), leaving out generated files such as `.sqlx/` and lockfiles
- the issue, `issue-todo read N`
- the repo's coding standards
- the parts of its agent docs and design docs that the change touches

When the orchestrator passes a review-prompt pointer, read the prompt file it names and follow it, under both its headings, with the issue and the questions below. Otherwise review on two axes:

- **Standards**: does the change follow the repo's documented rules? Tests, migrations and rollback, generated files, docs kept in step.
- **Spec**: does it do what the issue asked, no more, no less? Edge cases, error paths, and anything that breaks a deploy, a rollback or the owner's tools.

Check every claim the change rests on against the source, the issue's own premise included. Run a targeted test when a claim hinges on it.
Answer each question the orchestrator asked, including whether the change should land or become a draft + question for the owner.

Report in at most 25 lines, findings most severe first. Each one gives file:line, the defect, a concrete failure scenario and a severity (important or nit).
Then list what you checked and found sound. Say "no findings" if none survive.
