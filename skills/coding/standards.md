# Coding standards

The global standards a reviewer holds a diff to, in priority order. A repo's own standards (`CODING_STANDARDS.md` and the like) win where they conflict.

1. Quality and correctness
    - Architecture that favors testability
    - Robust to unexpected scenarios & inputs
    - Avoid code smells
    - Code should explain itself, making comments often unnecessary
2. Long term maintainability
    - Simplicity
    - Readability
    - Small updates require small changes
    - Structure for efficient agent understanding
3. Security
    - Limit attack/abuse surface area
    - An error about a secret (key file, credential) names its source only: never the exception text, file contents or a repr of the value
    - Defense in depth
4. Performance
5. Scalability

## Code Quality

If small updates require a large change, and fixing it isn't straightforward, consider recommending that I run the architectural improvement skill.

Some ideas for how to think about keeping clean code, and avoiding code smells:

- Group functions into files based on related functionality
- No god functions/classes.
- Make sure the code is documented appropriately so that it is easy to pick up.
- Minimize the following gotchas:
  - Casts to get around type checking
  - Extra comments that a human wouldn't add.
  - Heavy nesting
  - If/Else ladders
  - A ton of helper methods
