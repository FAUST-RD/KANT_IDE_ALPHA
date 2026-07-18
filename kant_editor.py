"""KANT Editor entry point. Application code lives in the kant package."""
import datetime
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QProxyStyle, QSplashScreen, QStyle

from kant.model import parse_kant, serialize_kant, read_top_level_label_result, Node, KantParseError
from kant.fileio import is_safe_child_name
from kant.syntax import check_syntax, check_kant_markers
from kant.xref import build_xref
from kant.gitutil import parse_git_status
from kant.widgets import make_app_icon, make_app_pixmap, CodeEdit
from kant.mainwindow import MainWindow


CRASH_LOG_DIR = Path.home() / '.kant_ide' / 'crash_logs'

# [CST] TOOLTIP_DELAY_MS — deliberately longer than Qt's own default wake-up delay (~700ms) so
# every hover-help tooltip across the app (every command has one, see the docstrings/setToolTip
# calls throughout kant/*.py) only appears once the pointer clearly lingers, not on every
# incidental pass-over while moving the mouse across the toolbar
TOOLTIP_DELAY_MS = 1100


# [FN CATEGORY] _HoverDelayStyle — the one shared choke point for every tooltip in the app: Qt has
# no per-widget "wake-up delay" setting, only this global style hint, so a single QProxyStyle
# applied once at startup covers every QWidget.setToolTip and QAction.setToolTip already used
# throughout the codebase, without editing each widget individually.
# [FN] _HoverDelayStyle — QProxyStyle overriding only the tooltip wake-up delay
# [FN OPEN] _HoverDelayStyle
class _HoverDelayStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_DELAY_MS
        return super().styleHint(hint, option, widget, returnData)
# [FN CLOSED] _HoverDelayStyle


# [FN CATEGORY] _install_crash_handler — PySide6 routes an exception raised inside a Qt slot/event
# handler through sys.excepthook same as any other uncaught exception, so replacing it here is
# enough to catch both startup failures (_self_check, MainWindow construction) and runtime crashes
# during the event loop. Without this, a windowed launch (no attached console, e.g. a future
# double-clicked .exe) would just have the app vanish with zero visible error for the user.
# [FN] _install_crash_handler — writes a crash log and shows an error dialog on any uncaught exception
# [FN OPEN] _install_crash_handler
def _install_crash_handler():
    def handle(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)  # still visible when run from a console
        log_path = None
        try:
            CRASH_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = CRASH_LOG_DIR / f'crash_{datetime.datetime.now():%Y%m%d_%H%M%S}.log'
            log_path.write_text(text, encoding='utf-8')
        except OSError:
            pass
        app = QApplication.instance()
        if app is None:
            return
        detail = f'{exc_type.__name__}: {exc_value}'
        location = f'\n\nDettagli salvati in:\n{log_path}' if log_path else ''
        QMessageBox.critical(None, 'KANT IDE — errore imprevisto', f'{detail}{location}')

    sys.excepthook = handle
# [FN CLOSED] _install_crash_handler


# [FN CATEGORY] _self_check — smallest runnable check for the parser: parses a fixture with nested
# CLS/FN markers and asserts the tree shape + round-trip serialization are lossless
# [FN] _self_check — asserts parse_kant/serialize_kant round-trip a nested fixture
# [FN OPEN] _self_check
def _self_check():
    fixture = '\n'.join([
        '# [CLS CATEGORY] UserManager — creates and authenticates users',
        '# [CLS] UserManager — creates and authenticates users',
        '# [CLS OPEN] UserManager',
        'class UserManager:',
        '    # [FN CATEGORY] login — checks credentials, creates session',
        '    # [FN] login — checks credentials, creates session',
        '    # [FN OPEN] login',
        '    def login(self): pass',
        '    # [FN CLOSED] login',
        '# [CLS CLOSED] UserManager',
    ])
    tree = parse_kant(fixture)
    cls = next(c for c in tree.body if isinstance(c, Node))
    assert cls.tag == 'CLS' and cls.name == 'UserManager', 'CLS not parsed'
    fn = next(c for c in cls.body if isinstance(c, Node))
    assert fn.tag == 'FN' and fn.name == 'login', 'nested FN not parsed'
    assert cls.category_desc == 'creates and authenticates users', 'category_desc not extracted'

    # legacy (id-less) fixture: parsing stamps a freshly generated #id into every OPEN/CLOSED, so
    # the immediate round-trip is expected to differ from the original — but from that point on,
    # further parse/serialize cycles must be lossless (the id is never regenerated once it exists)
    migrated = serialize_kant(tree)
    assert migrated != fixture, 'expected #id to be stamped into a legacy (id-less) fixture'
    assert '#' + cls.uid in migrated, 'generated id not written back into the serialized source'
    assert serialize_kant(parse_kant(migrated)) == migrated, 'post-migration round-trip must be lossless'

    # a file that already carries #ids must round-trip byte-for-byte — an id is identity and is
    # never regenerated, even though Name/desc are just labels on it
    tagged_fixture = '\n'.join([
        '# [FN CATEGORY] login — checks credentials',
        '# [FN] login — checks credentials',
        '# [FN OPEN #a1b2c3d4] login',
        'def login(): pass',
        '# [FN CLOSED #a1b2c3d4] login',
    ])
    assert serialize_kant(parse_kant(tagged_fixture)) == tagged_fixture, 'existing #id was regenerated on round-trip'

    # regression: two sibling scopes with an identically-named element must get distinct uids,
    # since _reveal_section looks widgets up by uid, not by (tag, name)
    dupe_fixture = '\n'.join([
        '# [CLS OPEN] A', 'class A:',
        '  # [FN OPEN] process', '  def process(self): pass', '  # [FN CLOSED] process',
        '# [CLS CLOSED] A',
        '# [CLS OPEN] B', 'class B:',
        '  # [FN OPEN] process', '  def process(self): pass', '  # [FN CLOSED] process',
        '# [CLS CLOSED] B',
    ])
    dupe_tree = parse_kant(dupe_fixture)
    cls_a, cls_b = [c for c in dupe_tree.body if isinstance(c, Node)]
    fn_a = next(c for c in cls_a.body if isinstance(c, Node))
    fn_b = next(c for c in cls_b.body if isinstance(c, Node))
    assert fn_a.name == fn_b.name == 'process', 'dupe fixture setup wrong'
    assert fn_a.uid != fn_b.uid, 'same-named siblings got colliding uids'

    stray_tree = parse_kant("print('[FN OPEN] nope')\nprint('[FN CLOSED] nope')")
    assert not any(isinstance(c, Node) for c in stray_tree.body), 'marker inside string was parsed as KANT'
    js_tree = parse_kant('// [FN OPEN] init\nfunction init() {}\n// [FN CLOSED] init')
    assert next(c for c in js_tree.body if isinstance(c, Node)).name == 'init', 'JS comment marker not parsed'

    # strict nesting: a CLOSED must match the top of the stack exactly — no backward search, no
    # silent recovery. Each of these is a hard parse error, not a best-effort guess.
    try:
        parse_kant('# [CLS OPEN] A\n# [FN OPEN] f\n# [CLS CLOSED] A\n# [FN CLOSED] f')
        assert False, 'crossing/mismatched CLOSED was not rejected'
    except KantParseError:
        pass
    try:
        parse_kant('# [FN CLOSED] f')
        assert False, 'CLOSED with no matching OPEN was not rejected'
    except KantParseError:
        pass
    try:
        parse_kant('# [FN OPEN #aaa] f\ndef f(): pass\n# [FN CLOSED #bbb] f')
        assert False, 'mismatched #id between OPEN and CLOSED was not rejected'
    except KantParseError:
        pass
    try:
        parse_kant('# [FN OPEN] f\ndef f(): pass')
        assert False, 'unclosed OPEN at EOF was not rejected'
    except KantParseError:
        pass

    # check_syntax: balanced brackets pass, an unclosed one is caught, and one mentioned inside a
    # comment/string doesn't produce a false positive
    assert check_syntax('def f():\n    return (1 + [2, 3])')['ok'] is True, 'balanced brackets flagged as bad'
    assert check_syntax('def f(:\n    return 1')['ok'] is False, 'unbalanced brackets not caught'
    assert check_syntax('# a stray ) in a comment\nx = (1 + 2)')['ok'] is True, 'bracket inside comment caused false positive'

    # check_kant_markers: valid file passes; a duplicate #id across two independent (non-nested)
    # pairs is caught even though it never trips the stack-matching check; malformed nesting is
    # surfaced the same way (through parse_kant) rather than needing its own separate detection
    assert check_kant_markers(tagged_fixture)['ok'] is True, 'valid KANT markers flagged as bad'
    assert check_kant_markers('# [FN OPEN #abc12345] f\npass\n# [FN CLOSED #abc12345] f')['ok'] is True, 'modern #id marker not checked'
    dupe_id_fixture = '\n'.join([
        '# [FN OPEN #dead0001] a', 'def a(): pass', '# [FN CLOSED #dead0001] a',
        '# [FN OPEN #dead0001] b', 'def b(): pass', '# [FN CLOSED #dead0001] b',
    ])
    assert check_kant_markers(dupe_id_fixture)['ok'] is False, 'duplicate #id across the file was not caught'
    assert check_kant_markers('# [FN CLOSED] f')['ok'] is False, 'malformed nesting not surfaced by check_kant_markers'

    # INCOMING/OUTGOING: parsed off the lines right after CLOSED, and preserved on round-trip
    # (using an already-#id'd fixture so id-stamping doesn't also change the text here)
    io_fixture = '\n'.join([
        '# [FN CATEGORY] list_users — paginates using offset',
        '# [FN] list_users — GET /users, paginated list',
        '# [FN OPEN #f00dcafe] list_users',
        'def list_users(page): return page',
        '# [FN CLOSED #f00dcafe] list_users',
        '# [FN INCOMING] list_users — page, MAX_PAGE_SIZE',
        '# [FN OUTGOING] list_users — paginated user list',
    ])
    io_tree = parse_kant(io_fixture)
    fn = next(c for c in io_tree.body if isinstance(c, Node))
    assert fn.incoming == 'page, MAX_PAGE_SIZE', f'incoming not parsed: {fn.incoming!r}'
    assert fn.outgoing == 'paginated user list', f'outgoing not parsed: {fn.outgoing!r}'
    assert serialize_kant(io_tree) == io_fixture, 'incoming/outgoing round-trip mismatch'

    git_status = parse_git_status(' M kant_editor.py\n?? PROJECT_MAP.md\nR  old.py -> new.py\n')
    assert git_status['kant_editor.py'] == 'M', 'git modified status not parsed'
    assert git_status['PROJECT_MAP.md'] == '??', 'git untracked status not parsed'
    assert git_status['new.py'] == 'R', 'git rename target not parsed'
    assert is_safe_child_name('new_file.py') is True, 'safe filename rejected'
    assert is_safe_child_name('../x.py') is False, 'path traversal filename accepted'

    # build_xref: a cross-file call is a directed edge (alpha -> beta), a constant read is an
    # edge (alpha -> LIMIT), and a name mentioned only inside a comment/string is NOT an edge
    xref_a = parse_kant('\n'.join([
        '# [MOD OPEN] a.py',
        '# [CST OPEN] LIMIT', 'LIMIT = 10', '# [CST CLOSED] LIMIT',
        '# [FN OPEN] alpha', 'def alpha():', '    return beta() + LIMIT', '# [FN CLOSED] alpha',
        '# [MOD CLOSED] a.py',
    ]))
    xref_b = parse_kant('\n'.join([
        '# [MOD OPEN] b.py',
        '# [FN OPEN] beta', 'def beta():', '    return 1  # alpha mentioned only here', '# [FN CLOSED] beta',
        '# [MOD CLOSED] b.py',
    ]))
    xref = build_xref({'a.py': xref_a, 'b.py': xref_b})
    by_name = {el.name: el for el in xref.values()}
    assert {xref[k].name for k in by_name['alpha'].outgoing} == {'beta', 'LIMIT'}, 'xref outgoing edges wrong'
    assert {xref[k].name for k in by_name['beta'].incoming} == {'alpha'}, 'xref incoming edge wrong'
    assert by_name['beta'].outgoing == [], 'name inside a comment must not create an edge'
    assert by_name['LIMIT'].incoming == [f"a.py::{by_name['alpha'].uid}"], 'constant read edge wrong'

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / 'bad.py'
        bad.write_text('# [FN OPEN #abc12345] f\npass\n', encoding='utf-8')
        label, error = read_top_level_label_result(str(bad))
        assert label is None and error is not None, 'invalid KANT file was not distinguished from untagged'

    print('KANT selfCheck: OK')
# [FN CLOSED] _self_check


# [FN CATEGORY] _make_splash — the native Qt splash reuses the bundled app logo and stays above
# startup work; QSplashScreen.finish closes it only after the main window is visible.
# [FN] _make_splash — builds the large startup logo window
# [FN OPEN] _make_splash
def _make_splash():
    return QSplashScreen(make_app_pixmap(320), Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
# [FN CLOSED] _make_splash


def main():
    _install_crash_handler()
    app = QApplication(sys.argv)
    app.setStyle(_HoverDelayStyle(app.style()))
    app.setWindowIcon(make_app_icon())
    splash = _make_splash()
    splash.show()
    app.processEvents()
    _self_check()
    window = MainWindow()
    window.show()
    splash.finish(window)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
