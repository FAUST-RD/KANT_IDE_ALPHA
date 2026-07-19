"""External-toolchain-backed exact scanners for kant/skeleton.py.

Each scanner shells out to the target language's OWN compiler/toolchain — already installed
because the user is working in that language — the same "detect an installed tool, use it, fall
back gracefully if it's missing" pattern kant/mainwindow.py already uses for black/ruff formatting
(_format_with_external_tool). Every scanner degrades to returning None on ANY failure — missing
binary, a crash in the helper program, output that doesn't parse as expected JSON — so a bug here
can never do worse than kant/skeleton.py's already-working regex fallback, only better when the
toolchain is present and cooperates.

Nothing here touches the network. Go's go/parser and Java's Compiler Tree API are both stdlib —
no package fetch needed once the SDK itself is installed. C#'s scanner references the .NET SDK's
own bundled Roslyn compiler assemblies directly (the same DLLs that power `csc.exe`), compiled
with `csc` against local reference assemblies only — no NuGet restore. The JS/TS scanner requires
an already-installed `typescript` package, found by walking up from the target file the way
Node's own module resolution would (a project's own node_modules) or via the global npm root —
never installed on the fly. C++'s uses `clang -Xclang -ast-dump=json`, a compiler flag, no
libclang bindings; its JSON shape is the least stable of the five (LLVM documents ast-dump=json
as not a stable API across versions), so its parser is deliberately the most defensive — any
unexpected shape just yields no elements for that node rather than a guess.

C#/TypeScript scanning was verified end-to-end against a real local .NET SDK + a real installed
`typescript` package. Go/Java/C++ were written with the same care but the corresponding toolchain
was not available to run in the environment this was built in — see kant/skeleton.py's own
docstring for the honest scope note.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'kant-skeleton-toolchains')
_TIMEOUT = 20
_TESTISH_RE = re.compile(r'(?i)^test|test$')


def _run_json(args, timeout=_TIMEOUT):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


# [FN] elements_from_json — validated JSON-list -> SkeletonElement conversion, shared by every
# scanner below; a malformed entry (missing key, wrong type) is dropped rather than raised on, so
# one bad item from a helper program doesn't discard everything else it found
# [FN OPEN] elements_from_json
def elements_from_json(items):
    from kant.skeleton import SkeletonElement
    out = []
    for item in items:
        try:
            out.append(SkeletonElement(
                str(item['tag']), str(item['name']),
                int(item['start_line']), int(item['end_line']), int(item.get('depth', 0)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out
# [FN CLOSED] elements_from_json


def _write_cached(name, content):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    digest = hashlib.sha1(content.encode('utf-8')).hexdigest()[:12]
    stem, ext = os.path.splitext(name)
    path = os.path.join(_CACHE_DIR, f'{stem}-{digest}{ext}')
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
    return path


def _temp_source(text, suffix):
    with tempfile.NamedTemporaryFile('w', suffix=suffix, delete=False, encoding='utf-8', newline='\n') as f:
        f.write(text)
        return f.name


# ---------------------------------------------------------------------------------------------
# Go — exact, via go/parser + go/ast (stdlib). Verified by careful reading of the Go standard
# library docs, not by running `go` (not installed in the environment this was built in).
# ---------------------------------------------------------------------------------------------

_GO_HELPER_SRC = r'''package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"regexp"
)

type elem struct {
	Tag       string `json:"tag"`
	Name      string `json:"name"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
	Depth     int    `json:"depth"`
}

var testish = regexp.MustCompile(`(?i)^test|test$`)

func main() {
	path := os.Args[1]
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		fmt.Println("[]")
		return
	}
	out := []elem{}
	for _, decl := range f.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			tag := "FN"
			if testish.MatchString(d.Name.Name) {
				tag = "TST"
			}
			out = append(out, elem{tag, d.Name.Name,
				fset.Position(d.Pos()).Line, fset.Position(d.End()).Line, 0})
		case *ast.GenDecl:
			for _, spec := range d.Specs {
				switch s := spec.(type) {
				case *ast.TypeSpec:
					tag := "CLS"
					if _, ok := s.Type.(*ast.InterfaceType); ok {
						tag = "TYP"
					}
					out = append(out, elem{tag, s.Name.Name,
						fset.Position(s.Pos()).Line, fset.Position(s.End()).Line, 0})
				case *ast.ValueSpec:
					tag := "VAR"
					if d.Tok == token.CONST {
						tag = "CST"
					}
					start := fset.Position(s.Pos()).Line
					end := fset.Position(s.End()).Line
					for _, name := range s.Names {
						if name.Name != "_" {
							out = append(out, elem{tag, name.Name, start, end, 0})
						}
					}
				}
			}
		}
	}
	data, err := json.Marshal(out)
	if err != nil {
		fmt.Println("[]")
		return
	}
	fmt.Println(string(data))
}
'''


def scan_go(text, file_path):
    if shutil.which('go') is None:
        return None
    helper = _write_cached('kant_go_scan.go', _GO_HELPER_SRC)
    temp_path = _temp_source(text, '.go')
    try:
        data = _run_json(['go', 'run', helper, temp_path])
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return elements_from_json(data) if data is not None else None


# ---------------------------------------------------------------------------------------------
# TypeScript/JavaScript — exact, via a real, already-installed `typescript` package (its parser
# handles plain .js too). Verified end-to-end against a locally installed typescript package.
# ---------------------------------------------------------------------------------------------

_TS_HELPER_SRC = r'''const ts = require(process.argv[2]);
const fs = require('fs');
const text = fs.readFileSync(process.argv[3], 'utf8');
const fileName = process.argv[4];
const isTsx = /\.tsx$/i.test(fileName);
const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, isTsx ? ts.ScriptKind.TSX : undefined);

function lineOf(pos) {
  return sf.getLineAndCharacterOfPosition(pos).line + 1;
}
function testish(name) {
  return /^test/i.test(name) || /test$/i.test(name);
}

const out = [];
function emit(tag, name, node, depth) {
  out.push({ tag, name, start_line: lineOf(node.getStart(sf)), end_line: lineOf(node.getEnd()), depth });
}

for (const stmt of sf.statements) {
  if (ts.isFunctionDeclaration(stmt) && stmt.name) {
    emit(testish(stmt.name.text) ? 'TST' : 'FN', stmt.name.text, stmt, 0);
  } else if (ts.isClassDeclaration(stmt) && stmt.name) {
    emit('CLS', stmt.name.text, stmt, 0);
    for (const member of stmt.members) {
      if (ts.isMethodDeclaration(member) && member.name && ts.isIdentifier(member.name)) {
        const mname = member.name.text;
        emit(testish(mname) ? 'TST' : 'FN', mname, member, 1);
      }
    }
  } else if (ts.isInterfaceDeclaration(stmt)) {
    emit('TYP', stmt.name.text, stmt, 0);
  } else if (ts.isTypeAliasDeclaration(stmt)) {
    emit('TYP', stmt.name.text, stmt, 0);
  } else if (ts.isEnumDeclaration(stmt)) {
    emit('TYP', stmt.name.text, stmt, 0);
  } else if (ts.isVariableStatement(stmt)) {
    const isConst = (stmt.declarationList.flags & ts.NodeFlags.Const) !== 0;
    for (const decl of stmt.declarationList.declarations) {
      if (ts.isIdentifier(decl.name)) {
        out.push({
          tag: isConst ? 'CST' : 'VAR', name: decl.name.text,
          start_line: lineOf(stmt.getStart(sf)), end_line: lineOf(stmt.getEnd()), depth: 0,
        });
      }
    }
  }
}
process.stdout.write(JSON.stringify(out));
'''


# [FN CATEGORY] _find_typescript — walks up from the target file's own directory the same way
# Node's own require() resolution would, looking for a project-local node_modules/typescript —
# the common case for any real JS/TS project, which lists it as a direct devDependency. Falls
# back to the global npm root (`npm root -g`) only if nothing project-local is found; never
# installs anything.
# [FN] _find_typescript — locates an already-installed typescript.js, or None
# [FN OPEN] _find_typescript
def _find_typescript(file_path):
    directory = os.path.dirname(os.path.abspath(file_path)) or os.getcwd()
    while True:
        candidate = os.path.join(directory, 'node_modules', 'typescript', 'lib', 'typescript.js')
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    npm = shutil.which('npm')
    if npm is None:
        return None
    try:
        result = subprocess.run([npm, 'root', '-g'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    global_root = result.stdout.strip()
    if not global_root:
        return None
    candidate = os.path.join(global_root, 'typescript', 'lib', 'typescript.js')
    return candidate if os.path.isfile(candidate) else None
# [FN CLOSED] _find_typescript


def scan_typescript(text, file_path):
    if shutil.which('node') is None:
        return None
    ts_path = _find_typescript(file_path)
    if ts_path is None:
        return None
    helper = _write_cached('kant_ts_scan.js', _TS_HELPER_SRC)
    ext = os.path.splitext(file_path)[1] or '.ts'
    temp_path = _temp_source(text, ext)
    try:
        data = _run_json(['node', helper, ts_path, temp_path, os.path.basename(file_path)])
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return elements_from_json(data) if data is not None else None


# ---------------------------------------------------------------------------------------------
# C# — exact, via Roslyn (Microsoft.CodeAnalysis.CSharp), the same compiler assemblies that power
# `csc.exe` — referenced directly from the .NET SDK's own install, compiled once and cached.
# No NuGet restore. Verified end-to-end against a real local .NET SDK install.
# ---------------------------------------------------------------------------------------------

_CS_HELPER_SRC = r'''using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Collections.Generic;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

class KantScan {
    static readonly Regex Testish = new Regex("^test|test$", RegexOptions.IgnoreCase);

    static void Main(string[] args) {
        string path = args[0];
        string text = File.ReadAllText(path);
        var tree = CSharpSyntaxTree.ParseText(text);
        var root = tree.GetCompilationUnitRoot();
        var results = new List<Dictionary<string, object>>();

        void Emit(string tag, string name, SyntaxNode node, int depth) {
            var span = tree.GetLineSpan(node.Span);
            results.Add(new Dictionary<string, object> {
                {"tag", tag}, {"name", name},
                {"start_line", span.StartLinePosition.Line + 1},
                {"end_line", span.EndLinePosition.Line + 1},
                {"depth", depth},
            });
        }

        void WalkMembers(SyntaxList<MemberDeclarationSyntax> members, int depth) {
            foreach (var member in members) {
                switch (member) {
                    case NamespaceDeclarationSyntax ns:
                        WalkMembers(ns.Members, depth);
                        break;
                    case FileScopedNamespaceDeclarationSyntax fns:
                        WalkMembers(fns.Members, depth);
                        break;
                    case ClassDeclarationSyntax cls:
                        Emit("CLS", cls.Identifier.Text, cls, depth);
                        WalkMembers(cls.Members, depth + 1);
                        break;
                    case StructDeclarationSyntax st:
                        Emit("CLS", st.Identifier.Text, st, depth);
                        WalkMembers(st.Members, depth + 1);
                        break;
                    case InterfaceDeclarationSyntax iface:
                        Emit("TYP", iface.Identifier.Text, iface, depth);
                        break;
                    case EnumDeclarationSyntax en:
                        Emit("TYP", en.Identifier.Text, en, depth);
                        break;
                    case RecordDeclarationSyntax rec:
                        Emit("CLS", rec.Identifier.Text, rec, depth);
                        WalkMembers(rec.Members, depth + 1);
                        break;
                    case MethodDeclarationSyntax method:
                        bool isTestAttr = method.AttributeLists
                            .SelectMany(al => al.Attributes)
                            .Any(a => Testish.IsMatch(a.Name.ToString()));
                        bool isTest = isTestAttr || Testish.IsMatch(method.Identifier.Text);
                        Emit(isTest ? "TST" : "FN", method.Identifier.Text, method, depth);
                        break;
                    case FieldDeclarationSyntax field:
                        bool isConst = field.Modifiers.Any(m => m.IsKind(SyntaxKind.ConstKeyword));
                        bool isReadonly = field.Modifiers.Any(m => m.IsKind(SyntaxKind.ReadOnlyKeyword));
                        string tag = (isConst || isReadonly) ? "CST" : "VAR";
                        foreach (var v in field.Declaration.Variables) {
                            Emit(tag, v.Identifier.Text, field, depth);
                        }
                        break;
                }
            }
        }

        WalkMembers(root.Members, 0);
        Console.WriteLine(JsonSerializer.Serialize(results));
    }
}
'''


def _dotnet_sdk_info():
    dotnet = shutil.which('dotnet')
    if dotnet is None:
        return None
    try:
        sdks = subprocess.run([dotnet, '--list-sdks'], capture_output=True, text=True, timeout=10)
        runtimes = subprocess.run([dotnet, '--list-runtimes'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sdk_line = next((line for line in sdks.stdout.splitlines() if line.strip()), None)
    if not sdk_line or '[' not in sdk_line:
        return None
    sdk_version, _, rest = sdk_line.partition('[')
    sdk_version = sdk_version.strip()
    sdk_root = rest.rstrip('] \r\n')
    dotnet_root = os.path.dirname(sdk_root)
    roslyn_dir = os.path.join(sdk_root, sdk_version, 'Roslyn', 'bincore')
    csc = os.path.join(roslyn_dir, 'csc.dll')
    if not os.path.isfile(csc):
        return None
    runtime_versions = [
        line.split()[1] for line in runtimes.stdout.splitlines()
        if line.startswith('Microsoft.NETCore.App ') and len(line.split()) > 1
    ]
    if not runtime_versions:
        return None
    try:
        runtime_version = sorted(runtime_versions, key=lambda v: [int(p) for p in v.split('.')])[-1]
    except ValueError:
        return None
    ref_dirs = sorted(glob.glob(os.path.join(dotnet_root, 'packs', 'Microsoft.NETCore.App.Ref', '*', 'ref', 'net*.0')))
    if not ref_dirs:
        return None
    return {
        'dotnet': dotnet, 'roslyn_dir': roslyn_dir, 'csc': csc,
        'runtime_version': runtime_version, 'ref_dir': ref_dirs[-1],
    }


def _compiled_cs_scanner(info):
    digest = hashlib.sha1((_CS_HELPER_SRC + info['runtime_version']).encode('utf-8')).hexdigest()[:12]
    out_dir = os.path.join(_CACHE_DIR, 'csharp-' + digest)
    dll = os.path.join(out_dir, 'KantScan.dll')
    if os.path.isfile(dll):
        return dll
    os.makedirs(out_dir, exist_ok=True)
    src_path = os.path.join(out_dir, 'KantScan.cs')
    with open(src_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(_CS_HELPER_SRC)
    ref_dlls = glob.glob(os.path.join(info['ref_dir'], '*.dll'))
    args = [
        info['dotnet'], 'exec', info['csc'], '-nostdlib+', '-noconfig', '/target:exe', f'/out:{dll}',
        f'/reference:{os.path.join(info["roslyn_dir"], "Microsoft.CodeAnalysis.dll")}',
        f'/reference:{os.path.join(info["roslyn_dir"], "Microsoft.CodeAnalysis.CSharp.dll")}',
    ] + [f'/reference:{r}' for r in ref_dlls] + [src_path]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not os.path.isfile(dll):
        return None
    # csc only checks these Roslyn assemblies at compile time — the CLR still has to load them
    # when KantScan.dll actually runs, so they need to sit next to it for the default probing path
    for name in ('Microsoft.CodeAnalysis.dll', 'Microsoft.CodeAnalysis.CSharp.dll'):
        shutil.copy(os.path.join(info['roslyn_dir'], name), out_dir)
    runtime_config = {
        'runtimeOptions': {
            'tfm': 'net' + '.'.join(info['runtime_version'].split('.')[:2]),
            'framework': {'name': 'Microsoft.NETCore.App', 'version': info['runtime_version']},
        },
    }
    with open(os.path.join(out_dir, 'KantScan.runtimeconfig.json'), 'w', encoding='utf-8') as f:
        json.dump(runtime_config, f)
    return dll


def scan_csharp(text, file_path):
    info = _dotnet_sdk_info()
    if info is None:
        return None
    dll = _compiled_cs_scanner(info)
    if dll is None:
        return None
    temp_path = _temp_source(text, '.cs')
    try:
        data = _run_json([info['dotnet'], 'exec', dll, temp_path])
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return elements_from_json(data) if data is not None else None


# ---------------------------------------------------------------------------------------------
# Java — exact, via the JDK's own Compiler Tree API (com.sun.source.*), a stable public surface
# meant for external tool use, no NuGet/Maven/network involved. Written carefully but the JDK
# was not available to actually run this in the environment this was built in.
# ---------------------------------------------------------------------------------------------

_JAVA_HELPER_SRC = r'''import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import javax.lang.model.element.Modifier;
import java.util.*;
import java.util.regex.Pattern;

public class KantScan {
    static final Pattern TESTISH = Pattern.compile("(?i)^test|test$");

    public static void main(String[] args) throws Exception {
        String path = args[0];
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager fm = compiler.getStandardFileManager(null, null, null);
        Iterable<? extends JavaFileObject> units =
            fm.getJavaFileObjectsFromStrings(Collections.singletonList(path));
        JavacTask task = (JavacTask) compiler.getTask(null, fm, null, null, null, units);
        Iterable<? extends CompilationUnitTree> trees = task.parse();
        Trees treesUtil = Trees.instance(task);

        StringBuilder out = new StringBuilder("[");
        boolean[] first = {true};

        for (CompilationUnitTree cu : trees) {
            LineMap lineMap = cu.getLineMap();
            SourcePositions positions = treesUtil.getSourcePositions();
            for (Tree decl : cu.getTypeDecls()) {
                if (!(decl instanceof ClassTree)) continue;
                ClassTree ct = (ClassTree) decl;
                emit(out, first, tagForClass(ct), ct.getSimpleName().toString(), ct, cu, lineMap, positions, 0);
                for (Tree member : ct.getMembers()) {
                    if (member instanceof MethodTree) {
                        MethodTree mt = (MethodTree) member;
                        String name = mt.getName().toString();
                        if (name.equals("<init>")) continue;
                        boolean isTest = TESTISH.matcher(name).find() || hasTestish(mt.getModifiers());
                        emit(out, first, isTest ? "TST" : "FN", name, mt, cu, lineMap, positions, 1);
                    } else if (member instanceof VariableTree) {
                        VariableTree vt = (VariableTree) member;
                        boolean isConst = vt.getModifiers().getFlags().contains(Modifier.FINAL);
                        emit(out, first, isConst ? "CST" : "VAR", vt.getName().toString(), vt, cu, lineMap, positions, 1);
                    }
                }
            }
        }
        out.append("]");
        System.out.println(out);
    }

    static boolean hasTestish(ModifiersTree mods) {
        for (AnnotationTree a : mods.getAnnotations()) {
            if (TESTISH.matcher(a.getAnnotationType().toString()).find()) return true;
        }
        return false;
    }

    static String tagForClass(ClassTree ct) {
        switch (ct.getKind()) {
            case INTERFACE: return "TYP";
            case ENUM: return "TYP";
            default: return "CLS";
        }
    }

    static void emit(StringBuilder out, boolean[] first, String tag, String name, Tree node,
                      CompilationUnitTree cu, LineMap lineMap, SourcePositions positions, int depth) {
        long startPos = positions.getStartPosition(cu, node);
        long endPos = positions.getEndPosition(cu, node);
        if (startPos < 0 || endPos < 0) return;
        long startLine = lineMap.getLineNumber(startPos);
        long endLine = lineMap.getLineNumber(endPos);
        if (!first[0]) out.append(",");
        first[0] = false;
        out.append("{\"tag\":\"").append(tag).append("\",\"name\":\"").append(jsonEscape(name))
           .append("\",\"start_line\":").append(startLine)
           .append(",\"end_line\":").append(endLine)
           .append(",\"depth\":").append(depth).append("}");
    }

    static String jsonEscape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
'''

# com.sun.source.* is the JDK's own stable "Compiler Tree API" surface (meant for external
# tools), but still lives inside the jdk.compiler module and needs an explicit export on modern
# module-system JDKs; harmless to pass on older ones
_JAVA_ADD_EXPORTS = [
    '--add-exports', 'jdk.compiler/com.sun.source.tree=ALL-UNNAMED',
    '--add-exports', 'jdk.compiler/com.sun.source.util=ALL-UNNAMED',
]


def _compiled_java_scanner():
    javac = shutil.which('javac')
    java = shutil.which('java')
    if javac is None or java is None:
        return None
    digest = hashlib.sha1(_JAVA_HELPER_SRC.encode('utf-8')).hexdigest()[:12]
    out_dir = os.path.join(_CACHE_DIR, 'java-' + digest)
    class_file = os.path.join(out_dir, 'KantScan.class')
    if os.path.isfile(class_file):
        return java, out_dir
    os.makedirs(out_dir, exist_ok=True)
    src_path = os.path.join(out_dir, 'KantScan.java')
    with open(src_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(_JAVA_HELPER_SRC)
    try:
        result = subprocess.run(
            [javac, *_JAVA_ADD_EXPORTS, '-d', out_dir, src_path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not os.path.isfile(class_file):
        return None
    return java, out_dir


def scan_java(text, file_path):
    compiled = _compiled_java_scanner()
    if compiled is None:
        return None
    java, out_dir = compiled
    temp_path = _temp_source(text, '.java')
    try:
        data = _run_json([java, *_JAVA_ADD_EXPORTS, '-cp', out_dir, 'KantScan', temp_path])
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return elements_from_json(data) if data is not None else None


# ---------------------------------------------------------------------------------------------
# C++ — via `clang -Xclang -ast-dump=json`, a compiler flag, no libclang bindings. LLVM documents
# this JSON shape as NOT a stable API across clang versions (unlike the other four languages'
# APIs, which are all meant for external consumption) — this parser is deliberately the most
# defensive of the five: any node shape it doesn't recognize is skipped, never guessed at.
# clang was not available to actually run this in the environment this was built in.
# ---------------------------------------------------------------------------------------------

def scan_cpp(text, file_path):
    clang = shutil.which('clang') or shutil.which('clang++')
    if clang is None:
        return None
    ext = os.path.splitext(file_path)[1].lower() or '.cpp'
    temp_path = _temp_source(text, ext)
    try:
        try:
            result = subprocess.run(
                [clang, '-Xclang', '-ast-dump=json', '-fsyntax-only', '-fno-color-diagnostics', temp_path],
                capture_output=True, text=True, timeout=_TIMEOUT, encoding='utf-8', errors='replace',
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not result.stdout.strip():
            return None
        try:
            root = json.loads(result.stdout)
        except ValueError:
            return None
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    try:
        return _elements_from_clang_json(root)
    except (KeyError, TypeError, AttributeError):
        return None


def _elements_from_clang_json(root):
    from kant.skeleton import SkeletonElement
    elements = []
    last_line = [1]

    def line_of(loc):
        # clang only emits "line" when it changes from the previously-dumped node — everything in
        # between inherits the last one seen, by clang's own documented dump convention
        if isinstance(loc, dict) and loc.get('line') is not None:
            last_line[0] = loc['line']
        return last_line[0]

    def walk_top_level(nodes):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get('kind')
            name = node.get('name')
            if kind == 'NamespaceDecl':
                walk_top_level(node.get('inner') or [])
                continue
            if not name or 'loc' not in node:
                continue
            loc = node.get('loc') or {}
            if 'file' in loc:
                continue  # pulled in from a different (e.g. system header) file, not this one
            rng = node.get('range') or {}
            begin = rng.get('begin') or loc
            end = rng.get('end') or begin
            start_line = line_of(begin)
            end_line = max(line_of(end), start_line)
            if kind in ('FunctionDecl', 'CXXMethodDecl'):
                tag = 'TST' if _TESTISH_RE.search(name) else 'FN'
                elements.append(SkeletonElement(tag, name, start_line, end_line, 0))
            elif kind in ('CXXRecordDecl', 'RecordDecl') and node.get('tagUsed') in ('class', 'struct'):
                elements.append(SkeletonElement('CLS', name, start_line, end_line, 0))
            elif kind == 'EnumDecl':
                elements.append(SkeletonElement('TYP', name, start_line, end_line, 0))
            elif kind == 'VarDecl':
                qual_type = ((node.get('type') or {}).get('qualType') or '')
                tag = 'CST' if 'const' in qual_type else 'VAR'
                elements.append(SkeletonElement(tag, name, start_line, end_line, 0))

    walk_top_level(root.get('inner') or [])
    return elements


# ---------------------------------------------------------------------------------------------

SCANNERS = {
    'Go': scan_go,
    'TypeScript': scan_typescript,
    'JavaScript': scan_typescript,
    'C#': scan_csharp,
    'Java': scan_java,
    'C++': scan_cpp,
}
