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
    member_hints: dict = field(default_factory=dict)  # key -> stable xref fields used for recovery
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
    return [
        Grouping(
            id=g['id'],
            name=g['name'],
            members=list(g.get('members', ())),
            member_hints=dict(g.get('member_hints', {})) if isinstance(g.get('member_hints', {}), dict) else {},
        )
        for g in groups if g.get('id') and g.get('name')
    ]
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
def add_member(project_root, group_id, key, hint=None):
    groupings = load_groupings(project_root)
    for grouping in groupings:
        if grouping.id == group_id:
            if key not in grouping.members:
                grouping.members.append(key)
            if hint:
                grouping.member_hints[key] = dict(hint)
            break
    save_groupings(project_root, groupings)
    return groupings
# [FN CLOSED] add_member


def member_hint(element):
    """Minimal identity snapshot for recovering one grouped xref element after edits."""
    if element is None:
        return {}
    return {
        'file': element.file,
        'uid': element.uid,
        'tag': element.tag,
        'name': element.name,
        'order': element.order,
    }


def reconcile_groupings(groupings, xref):
    """Repair stale member keys only when the current xref has one unambiguous match."""
    indexes = ({}, {}, {})
    for element in xref.values():
        identities = (
            element.uid,
            (element.file, element.tag, element.name),
            (element.file, element.tag, element.order),
        )
        for index, identity in zip(indexes, identities):
            index.setdefault(identity, []).append(element)

    changed = False
    for grouping in groupings:
        old_members = list(grouping.members)
        old_hints = dict(grouping.member_hints)
        members = []
        hints = {}
        for old_key in old_members:
            key = old_key
            element = xref.get(key)
            hint = old_hints.get(old_key, {})
            if element is None and hint:
                identities = (
                    hint.get('uid'),
                    (hint.get('file'), hint.get('tag'), hint.get('name')),
                    (hint.get('file'), hint.get('tag'), hint.get('order')),
                )
                for index, identity in zip(indexes, identities):
                    parts = identity if isinstance(identity, tuple) else (identity,)
                    candidates = index.get(identity, ()) if None not in parts else ()
                    if len(candidates) == 1:
                        element = candidates[0]
                        key = element.key
                        break
            if key not in members:
                members.append(key)
                current_hint = member_hint(element) or hint
                if current_hint:
                    hints[key] = current_hint
        if members != old_members or hints != old_hints:
            grouping.members = members
            grouping.member_hints = hints
            changed = True
    return changed


def load_reconciled_groupings(project_root, xref):
    """Load groups and persist deterministic key repairs and refreshed member hints."""
    groupings = load_groupings(project_root)
    if reconcile_groupings(groupings, xref):
        save_groupings(project_root, groupings)
    return groupings


# [FN CATEGORY] remap_member_key — rewrites the rel_path portion of one xref-style key
# ('<rel_path>::<uid>') when it falls under old_rel, leaving the uid untouched. For a file rename
# (is_dir=False) only an exact rel_path match qualifies; for a folder rename (is_dir=True) both the
# folder's own key (rare — groupings hold elements, which live inside files, not bare folders) and
# every key nested under it qualify, mirroring the same old_path/os.sep prefix test
# kant/workspace.py:_rename_tree_item already uses to find affected open tabs. Pure and total: a key
# that doesn't match old_rel is returned unchanged, so a caller can map this over every member
# without a separate "does this apply" branch, and a second call with the same (old_rel, new_rel)
# after the first is a no-op — nothing left starts with old_rel anymore.
# [FN] remap_member_key — key with its rel_path rewritten if under old_rel, else key unchanged
# [FN OPEN] remap_member_key
def remap_member_key(key, old_rel, new_rel, is_dir):
    if '::' not in key:
        return key
    rel, uid = key.rsplit('::', 1)
    if is_dir:
        if rel == old_rel:
            new_path = new_rel
        elif rel.startswith(old_rel + '/'):
            new_path = new_rel + rel[len(old_rel):]
        else:
            return key
    elif rel != old_rel:
        return key
    else:
        new_path = new_rel
    return f'{new_path}::{uid}'
# [FN CLOSED] remap_member_key


# [FN CATEGORY] migrate_member_paths — after KANT IDE renames a file or folder, updates every
# Grouping member key whose path fell under the old name, preserving the uid and leaving unrelated
# members untouched. Saves only if something actually changed (idempotent: a repeat call with the
# same old_rel/new_rel finds nothing left to remap and skips the write).
# [FN] migrate_member_paths — remaps Groupings member keys after a rename; True if anything changed
# [FN OPEN] migrate_member_paths
def migrate_member_paths(project_root, old_rel, new_rel, is_dir):
    groupings = load_groupings(project_root)
    changed = False
    for grouping in groupings:
        # dict.fromkeys dedupes while preserving order — if two distinct old keys ever remapped onto
        # the same new key (a rename target colliding with an existing member), keep one entry
        # instead of a stray duplicate; mirrors migrate_position_keys' own collision handling below
        remapped = []
        remapped_hints = {}
        for old_key in grouping.members:
            new_key = remap_member_key(old_key, old_rel, new_rel, is_dir)
            if new_key not in remapped:
                remapped.append(new_key)
            hint = dict(grouping.member_hints.get(old_key, {}))
            if hint:
                if new_key != old_key:
                    hint['file'] = new_key.rsplit('::', 1)[0]
                remapped_hints[new_key] = hint
        if remapped != grouping.members or remapped_hints != grouping.member_hints:
            grouping.members = remapped
            grouping.member_hints = remapped_hints
            changed = True
    if changed:
        save_groupings(project_root, groupings)
    return changed
# [FN CLOSED] migrate_member_paths
