---
name: coding
description: Coding standards, what to weigh and in what order, testing, commits. Load before writing or changing code, tests or build config, in any project.
---

# Coding

You code very fast. So when planning, weigh development cost ~0. Instead, prefer in rough order:

1. Quality and correctness
    - Red / Green testing
    - Great coverage: e2e and property testing (like python hypothesis)
    - Architecture that favors testability
    - Avoid code smells
    - Code should explain itself, making comments often unnecessary
2. Long term maintainability
    - Simplicity
    - Readability
    - Small updates require small changes
    - Structure for efficient agent understanding
3. Security
    - Limit attack/abuse surface area
    - Defense in depth
4. Performance
5. Scalability

Make logically isolated commits.

Use /diagnosing-bugs to debug.

Be an excellent engineer with high standards: if you notice issues, fix them. Examples: lint, test flakiness.
