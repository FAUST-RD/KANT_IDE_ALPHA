"""Cross-file element groupings — deterministic, no Qt, mirrors kant/pyenv.py's own boundary.

A grouping is an arbitrary, named collection of KANT elements (any tag — MOD/CLS/FN/TYP/CST/VAR/
CFG/TST — parent or child indifferently, from any file) bundled together independent of the
source tree's own nesting. Persisted as .kant/groupings.json, the same project-config directory
kant/pyenv.py's python.json already established as the convention for "project state that isn't
source content". Members are stored as xref-style keys ('<rel_path>::<uid>', see kant/xref.py's
XrefElement.key) — the same identifier the existing cross-reference graph and _navigate_to_element
already resolve, so a grouping's members are navigable for free, with no new lookup machinery.
"""
import json
import secrets
from dataclasses import dataclass, field, asdict
from pathlib import Path


CONFIG_DIRNAME = '.kant'
CONFIG_FILENAME = 'groupings.json'


# [TYP] Grouping — one named, arbitrary bundle of element keys
# [TYP OPEN] Grouping
@dataclass
class Grouping:
    id: str
    name: str
    members: list = field(default_factory=list)  # xref-style keys, '<rel_path>::<uid>'
# [TYP CLOSED] Grouping


def config_path(project_root):
    return Path(project_root) / CONFIG_DIRNAME / CONFIG_FILENAME


# [FN] load_groupings — every grouping saved for project_root, or [] if none/unreadable
# [FN OPEN] load_groupings
def load_groupings(project_root):
    try:
        data = json.loads(config_path(project_root).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    groups = data.get('groups', [])
    return [Grouping(id=g['id'], name=g['name'], members=list(g.get('members', ()))) for g in groups if g.get('id') and g.get('name')]
# [FN CLOSED] load_groupings


# [FN] save_groupings — writes every grouping for project_root to .kant/groupings.json
# [FN OPEN] save_groupings
def save_groupings(project_root, groupings):
    path = config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'groups': [asdict(g) for g in groupings]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
# [FN CLOSED] save_groupings


def new_grouping(name):
    return Grouping(id=secrets.token_hex(4), name=name.strip())


# [FN] add_member — adds a key to a grouping (by id) if not already present, saves, returns the
# updated list; a pure convenience wrapper so callers don't hand-roll the load/mutate/save sequence
# [FN OPEN] add_member
def add_member(project_root, group_id, key):
    groupings = load_groupings(project_root)
    for grouping in groupings:
        if grouping.id == group_id:
            if key not in grouping.members:
                grouping.members.append(key)
            break
    save_groupings(project_root, groupings)
    return groupings
# [FN CLOSED] add_member
