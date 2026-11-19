#!/bin/sh
# llm-toolbox's stand-in for File Drawer's upgrade script, which the skill's SKILL.md
# runs as `sh FOLDER/references/upgrade.sh VERSION`. This copy of the skill lives in
# llm-toolbox, so bin/filedrawer-skill-upgrade upgrades it there from its git tag and
# commits the upgrade, putting this file back in place of the tag's (its -h says how).
set -eu
here=$(cd "$(dirname "$0")" && pwd -P)
skill=$(basename "$(dirname "$here")")
exec "$here/../../../bin/filedrawer-skill-upgrade" "$skill" "$@"
