from __future__ import annotations

import argparse
import csv
import json
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


# ============================================================
# Logging / IO
# ============================================================

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def read_text_lossless(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: List[str] = []
        seen = set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        fieldnames = keys
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ============================================================
# Tree-sitter Rust parser loading
# ============================================================

class ParserLoadError(RuntimeError):
    pass


def load_rust_parser():
    """
    Supports several common Python tree-sitter packaging styles:
      1) tree_sitter + tree_sitter_rust
      2) tree_sitter + tree_sitter_languages

    The tree_sitter API changed over time, so assignment is made defensively.
    """
    try:
        from tree_sitter import Parser, Language  # type: ignore
    except Exception as e:
        raise ParserLoadError(
            "Missing tree_sitter. Install with: py -m pip install tree_sitter tree_sitter_rust"
        ) from e

    language = None
    errors = []

    # Preferred modern package.
    try:
        import tree_sitter_rust  # type: ignore
        raw_lang = tree_sitter_rust.language()
        try:
            language = Language(raw_lang)  # py-tree-sitter >= 0.22
        except Exception:
            language = raw_lang
    except Exception as e:
        errors.append(f"tree_sitter_rust failed: {e}")

    # Alternative older package.
    if language is None:
        try:
            from tree_sitter_languages import get_language  # type: ignore
            language = get_language("rust")
        except Exception as e:
            errors.append(f"tree_sitter_languages failed: {e}")

    if language is None:
        raise ParserLoadError(
            "Cannot load Rust grammar. Try:\n"
            "  py -m pip install tree_sitter tree_sitter_rust\n"
            "or:\n"
            "  py -m pip install tree_sitter tree_sitter_languages\n"
            "Details: " + " | ".join(errors)
        )

    parser = Parser()
    try:
        parser.set_language(language)
    except AttributeError:
        parser.language = language
    return parser


def parse_rust(parser: Any, code: str):
    return parser.parse(code.encode("utf-8"))


def parse_ok(parser: Any, code: str) -> bool:
    try:
        tree = parse_rust(parser, code)
        return not bool(tree.root_node.has_error)
    except Exception:
        return False


# ============================================================
# Tree helpers
# ============================================================

def node_text(code: str, node: Any) -> str:
    b = code.encode("utf-8")
    return b[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def walk(node: Any) -> Iterator[Any]:
    yield node
    for ch in getattr(node, "children", []):
        yield from walk(ch)


def find_first(node: Any, types: Sequence[str]) -> Optional[Any]:
    wanted = set(types)
    for n in walk(node):
        if n.type in wanted:
            return n
    return None


def find_all(node: Any, types: Sequence[str]) -> List[Any]:
    wanted = set(types)
    return [n for n in walk(node) if n.type in wanted]


def find_function_node(parser: Any, code: str) -> Optional[Any]:
    tree = parse_rust(parser, code)
    if tree.root_node.has_error:
        return None
    # In tree-sitter-rust, a free function/method item is normally function_item.
    return find_first(tree.root_node, ["function_item"])


def find_function_body_block(parser: Any, code: str) -> Optional[Any]:
    fn = find_function_node(parser, code)
    if fn is None:
        return None

    # Try named body field first.
    try:
        body = fn.child_by_field_name("body")
        if body is not None and body.type == "block":
            return body
    except Exception:
        pass

    # Fallback: last direct/descendant block inside the function item.
    blocks = [n for n in walk(fn) if n.type == "block"]
    if not blocks:
        return None
    return blocks[-1]


def direct_statement_nodes(block: Any) -> List[Any]:
    """
    Return direct named children in a function/block body that are likely statements.
    The exact type names vary slightly by grammar version; this accepts direct named
    nodes and filters out nested blocks themselves.
    """
    out = []
    for ch in getattr(block, "named_children", []):
        if ch.type in {"attribute_item"}:
            continue
        if ch.type == "block":
            continue
        out.append(ch)
    return out


def line_col_of_byte(code: str, byte_offset: int) -> Tuple[int, int]:
    prefix = code.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore")
    line = prefix.count("\n") + 1
    col = len(prefix.rsplit("\n", 1)[-1]) + 1
    return line, col


def apply_replacements_bytes(code: str, replacements: List[Tuple[int, int, str]]) -> str:
    """
    replacements are byte offsets: (start_byte, end_byte, replacement_text).
    Applied from right to left.
    """
    b = code.encode("utf-8")
    for start, end, repl in sorted(replacements, key=lambda x: x[0], reverse=True):
        b = b[:start] + repl.encode("utf-8") + b[end:]
    return b.decode("utf-8", errors="replace")


def indent_at_byte(code: str, byte_offset: int) -> str:
    prefix = code.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore")
    current_line = prefix.rsplit("\n", 1)[-1]
    m = re.match(r"^(\s*)", current_line)
    return m.group(1) if m else "    "


def insert_text_at_byte(code: str, byte_offset: int, insert_text: str) -> str:
    return apply_replacements_bytes(code, [(byte_offset, byte_offset, insert_text)])


def safe_id(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(s)).strip("_")
    if not s:
        return "bench"
    if s[0].isdigit():
        s = "B_" + s
    return s


# ============================================================
# Rust-ish tokenization and similarity metrics
# ============================================================

TOKEN_RE = re.compile(
    r"""
    (?P<block_comment>/\*.*?\*/)
  | (?P<line_comment>//[^\n]*)
  | (?P<raw_string>r\#*"(?:.|\n)*?"\#*)
  | (?P<byte_string>b"(?:\\.|[^"\\])*")
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<char>'(?:\\.|[^'\\])')
  | (?P<lifetime>'[A-Za-z_][A-Za-z0-9_]*)
  | (?P<number>\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)(?:u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize|f32|f64)?\b)
  | (?P<identifier>r\#[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)
  | (?P<operator>::|->|=>|==|!=|<=|>=|\+=|-=|\*=|/=|%=|&&|\|\||<<|>>|\.\.|::)
  | (?P<symbol>[{}()\[\];,.:?@+\-*/%<>=!~&|^#])
  | (?P<whitespace>\s+)
  | (?P<other>.)
    """,
    re.DOTALL | re.VERBOSE,
)

RUST_KEYWORDS = {
    "as", "break", "const", "continue", "crate", "else", "enum", "extern",
    "false", "fn", "for", "if", "impl", "in", "let", "loop", "match",
    "mod", "move", "mut", "pub", "ref", "return", "self", "Self", "static",
    "struct", "super", "trait", "true", "type", "unsafe", "use", "where",
    "while", "async", "await", "dyn",
}


def iter_token_kinds(code: str) -> Iterable[Tuple[str, str]]:
    for m in TOKEN_RE.finditer(code):
        kind = m.lastgroup or "other"
        text = m.group(0)
        if kind in {"block_comment", "line_comment", "whitespace"}:
            continue
        yield kind, text


def raw_tokens(code: str) -> List[str]:
    return [text for _, text in iter_token_kinds(code)]


def raw_line_units(code: str) -> List[str]:
    units = []
    for line in code.splitlines():
        toks = raw_tokens(line)
        if toks:
            units.append(" ".join(toks))
    return units


def norm_token(kind: str, text: str) -> Optional[str]:
    if kind in {"block_comment", "line_comment", "whitespace"}:
        return None
    if kind == "identifier":
        clean = text[2:] if text.startswith("r#") else text
        if clean in RUST_KEYWORDS:
            return clean
        return "ID"
    if kind == "lifetime":
        return "LIFETIME"
    if kind in {"string", "raw_string", "byte_string", "char"}:
        return "LIT_STR"
    if kind == "number":
        return "LIT_NUM"
    return text


def norm_tokens(code: str) -> List[str]:
    out = []
    for kind, text in iter_token_kinds(code):
        tok = norm_token(kind, text)
        if tok:
            out.append(tok)
    return out


def norm_line_units(code: str) -> List[str]:
    units = []
    for line in code.splitlines():
        toks = norm_tokens(line)
        if toks:
            units.append(" ".join(toks))
    return units


def multiset_overlap_max(tokens_a: List[str], tokens_b: List[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    a = sorted(tokens_a)
    b = sorted(tokens_b)
    i = j = shared = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            shared += 1
            i += 1
            j += 1
        elif a[i] < b[j]:
            i += 1
        else:
            j += 1
    return shared / max(len(a), len(b))


def line_sr_max(lines_a: List[str], lines_b: List[str], n: int = 3) -> float:
    if len(lines_a) < n or len(lines_b) < n:
        return 0.0
    blocks_a = {"\n".join(lines_a[i:i+n]) for i in range(len(lines_a) - n + 1)}
    blocks_b = {"\n".join(lines_b[i:i+n]) for i in range(len(lines_b) - n + 1)}
    if not blocks_a or not blocks_b:
        return 0.0
    return len(blocks_a & blocks_b) / max(len(blocks_a), len(blocks_b))


def longest_common_contiguous_run(tokens_a: List[str], tokens_b: List[str]) -> int:
    if not tokens_a or not tokens_b:
        return 0
    prev = [0] * (len(tokens_b) + 1)
    best = 0
    for i in range(1, len(tokens_a) + 1):
        cur = [0] * (len(tokens_b) + 1)
        ta = tokens_a[i - 1]
        for j in range(1, len(tokens_b) + 1):
            if ta == tokens_b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def run_ratio_min(tokens_a: List[str], tokens_b: List[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    return longest_common_contiguous_run(tokens_a, tokens_b) / max(1, min(len(tokens_a), len(tokens_b)))


def length_ratio(a: int, b: int) -> float:
    if a <= 0 or b <= 0:
        return float("inf")
    return max(a, b) / max(1, min(a, b))


def code_signature(code: str) -> str:
    return "\n".join(line.rstrip() for line in code.strip().splitlines())


def token_signature(tokens: List[str]) -> str:
    return " ".join(tokens)


# ============================================================
# Project loading
# ============================================================

def discover_projects_from_roots(source_root: Path, result_root: Path, functions_name: str) -> List[Dict[str, str]]:
    projects: List[Dict[str, str]] = []
    if not source_root.exists():
        raise FileNotFoundError(f"source_root not found: {source_root}")
    if not result_root.exists():
        raise FileNotFoundError(f"result_root not found: {result_root}")

    for result_dir in sorted([p for p in result_root.iterdir() if p.is_dir()]):
        name = result_dir.name
        functions_jsonl = result_dir / functions_name
        project_root = source_root / name
        if functions_jsonl.exists() and project_root.exists():
            projects.append({
                "project_name": name,
                "project_root": str(project_root),
                "functions_jsonl": str(functions_jsonl),
            })
    return projects


def load_projects(args: argparse.Namespace) -> List[Dict[str, str]]:
    if args.source_root and args.result_root:
        projects = discover_projects_from_roots(
            Path(args.source_root),
            Path(args.result_root),
            args.functions_name,
        )
        if not projects:
            raise RuntimeError("No projects found. Check --source-root / --result-root / --functions-name.")
        return projects

    if args.projects_csv:
        projects = []
        with Path(args.projects_csv).open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            required = {"project_name", "project_root", "functions_jsonl"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"projects csv missing fields: {missing}")
            for row in reader:
                if row.get("project_name") and row.get("project_root") and row.get("functions_jsonl"):
                    projects.append({
                        "project_name": row["project_name"].strip(),
                        "project_root": row["project_root"].strip(),
                        "functions_jsonl": row["functions_jsonl"].strip(),
                    })
        if not projects:
            raise RuntimeError("projects csv has no valid projects.")
        return projects

    if args.project_root and args.functions_jsonl:
        return [{
            "project_name": args.project_name or Path(args.project_root).name,
            "project_root": args.project_root,
            "functions_jsonl": args.functions_jsonl,
        }]

    raise ValueError("Use --source-root + --result-root, or --projects-csv, or --project-root + --functions-jsonl.")


def get_code_from_row(project_root: Path, row: Dict[str, Any]) -> Optional[str]:
    for key in ["code", "raw_code", "source", "source_code"]:
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v

    file_rel = row.get("file")
    start_line = row.get("start_line")
    end_line = row.get("end_line")
    if file_rel is None or start_line is None or end_line is None:
        return None

    file_path = project_root / str(file_rel)
    if not file_path.exists():
        return None

    lines = read_text_lossless(file_path).splitlines()
    s = max(int(start_line) - 1, 0)
    e = min(int(end_line), len(lines))
    if s >= e:
        return None
    return "\n".join(lines[s:e])


# ============================================================
# Seed checks and wrapping
# ============================================================

def macro_call_count(code: str) -> int:
    return len(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*!", code))


def extract_function_name(code: str) -> str:
    m = re.search(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)", code)
    return m.group(1) if m else ""


def has_self_parameter(code: str) -> bool:
    return bool(re.search(r"\bfn\s+\w+\s*\([^)]*\bself\b", code, flags=re.S))


def wrap_as_rust_file(code: str, bench_id: str) -> str:
    header = [
        "#![allow(dead_code)]",
        "#![allow(unused_variables)]",
        "#![allow(unused_mut)]",
        "#![allow(unreachable_code)]",
        "#![allow(non_camel_case_types)]",
        "#![allow(non_snake_case)]",
        "",
    ]
    if has_self_parameter(code):
        struct_name = "BenchType_" + safe_id(bench_id)
        return "\n".join(header + [
            f"struct {struct_name};",
            f"impl {struct_name} {{",
            code,
            "}",
            "",
        ])
    return "\n".join(header + [code, ""])


def seed_filter(parser: Any, code: str, args: argparse.Namespace) -> Tuple[bool, str]:
    if not parse_ok(parser, code):
        return False, "parser_error_seed"

    fn = find_function_node(parser, code)
    if fn is None:
        return False, "no_function_item"

    body = find_function_body_block(parser, code)
    if body is None:
        return False, "no_function_body"

    rtoks = raw_tokens(code)
    rlines = raw_line_units(code)
    if len(rtoks) < args.min_tokens:
        return False, "too_short_tokens"
    if len(rtoks) > args.max_tokens:
        return False, "too_long_tokens"
    if len(rlines) < args.min_lines:
        return False, "too_short_lines"

    if "macro_rules!" in code:
        return False, "macro_rules"
    if macro_call_count(code) >= args.macro_threshold:
        return False, "macro_heavy"
    if args.exclude_unsafe and re.search(r"\bunsafe\b", code):
        return False, "unsafe"
    if args.exclude_const_fn and re.search(r"\bconst\s+fn\b", code):
        return False, "const_fn"

    direct = direct_statement_nodes(body)
    if len(direct) < args.min_statements:
        return False, "too_few_statements"

    if args.free_functions_only and has_self_parameter(code):
        return False, "method_with_self"

    wrapped = wrap_as_rust_file(code, "seed_check")
    if not parse_ok(parser, wrapped):
        return False, "wrapper_parser_error"

    return True, "ok"


def exact_deduplicate(candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    selected = []
    rejected = []
    seen_code: Dict[str, str] = {}
    seen_tokens: Dict[str, str] = {}

    for cand in candidates:
        csig = code_signature(cand["code"])
        tsig = token_signature(cand["raw_tokens_for_benchmark"])
        gid = cand.get("source_func_global_id", "")

        if csig in seen_code:
            rejected.append({
                "project_name": cand.get("project_name", ""),
                "source_func_global_id": gid,
                "reason": "exact_raw_code_duplicate",
                "duplicate_of": seen_code[csig],
            })
            continue
        if tsig in seen_tokens:
            rejected.append({
                "project_name": cand.get("project_name", ""),
                "source_func_global_id": gid,
                "reason": "exact_raw_token_sequence_duplicate",
                "duplicate_of": seen_tokens[tsig],
            })
            continue

        selected.append(cand)
        seen_code[csig] = gid
        seen_tokens[tsig] = gid

    return selected, rejected


# ============================================================
# Parser-based mutation operators
# ============================================================

@dataclass
class MutationResult:
    rule_name: str
    clone_type: str
    subtype: str
    code: str
    position: str
    detail: str


def validate_mutation(parser: Any, code: str) -> bool:
    return parse_ok(parser, code) and parse_ok(parser, wrap_as_rust_file(code, "validate_mutation"))


def block_insert_after_open(parser: Any, code: str, lines: List[str]) -> Optional[Tuple[str, str]]:
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    insert_at = block.start_byte + 1
    indent = "    "
    text = "\n" + "\n".join(indent + x for x in lines) + "\n"
    return insert_text_at_byte(code, insert_at, text), "function_body_begin"


def block_insert_before_close(parser: Any, code: str, lines: List[str]) -> Optional[Tuple[str, str]]:
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    insert_at = max(block.start_byte + 1, block.end_byte - 1)
    indent = "    "
    text = "\n" + "\n".join(indent + x for x in lines) + "\n"
    return insert_text_at_byte(code, insert_at, text), "function_body_end"


def insert_after_middle_statement(parser: Any, code: str, lines: List[str]) -> Optional[Tuple[str, str]]:
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    stmts = direct_statement_nodes(block)
    if not stmts:
        return None
    target = stmts[len(stmts) // 2]
    insert_at = target.end_byte
    indent = indent_at_byte(code, target.start_byte)
    text = "\n" + "\n".join(indent + x for x in lines)
    return insert_text_at_byte(code, insert_at, text), f"after_statement_{target.type}"


def middle_statement_candidates(block: Any, avoid_edges: bool = True) -> List[Any]:
    """Return direct statement nodes, preferably excluding function-body edges.

    Type-3 statement/block insertion should not normally be placed immediately
    after the opening brace or directly before the closing brace, because such
    boundary edits are too easy for clone detectors and less representative of
    edits inside copied code. If the function is short, fall back conservatively.
    """
    stmts = direct_statement_nodes(block)
    if not stmts:
        return []
    if avoid_edges and len(stmts) >= 4:
        return stmts[1:-1]
    if avoid_edges and len(stmts) >= 2:
        return stmts[:-1]
    return stmts


def select_middle_statement(block: Any, avoid_edges: bool = True) -> Optional[Any]:
    cands = middle_statement_candidates(block, avoid_edges=avoid_edges)
    if not cands:
        return None
    return cands[len(cands) // 2]


def insert_after_statement_node(code: str, target: Any, lines: List[str]) -> Tuple[str, str]:
    insert_at = target.end_byte
    indent = indent_at_byte(code, target.start_byte)
    text = "\n" + "\n".join(indent + x for x in lines)
    return insert_text_at_byte(code, insert_at, text), f"after_statement_{target.type}"


def insert_after_internal_statement(parser: Any, code: str, lines: List[str]) -> Optional[Tuple[str, str]]:
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    target = select_middle_statement(block, avoid_edges=True)
    if target is None:
        return None
    return insert_after_statement_node(code, target, lines)


def select_two_separated_statements(block: Any, min_gap: int = 1) -> Optional[Tuple[Any, Any]]:
    cands = middle_statement_candidates(block, avoid_edges=True)
    if len(cands) < 2:
        return None
    i = max(0, len(cands) // 3)
    j = min(len(cands) - 1, (2 * len(cands)) // 3)
    if j <= i:
        j = min(len(cands) - 1, i + 1)
    # Prefer two visibly separated positions if available.
    if j - i < min_gap and len(cands) >= 3:
        i, j = 0, len(cands) - 1
    if i == j:
        return None
    return cands[i], cands[j]


def insert_after_two_statement_nodes(code: str, first: Any, first_lines: List[str], second: Any, second_lines: List[str]) -> Tuple[str, str]:
    reps: List[Tuple[int, int, str]] = []
    for target, lines in [(first, first_lines), (second, second_lines)]:
        indent = indent_at_byte(code, target.start_byte)
        text = "\n" + "\n".join(indent + x for x in lines)
        reps.append((target.end_byte, target.end_byte, text))
    mutated = apply_replacements_bytes(code, reps)
    return mutated, f"after_two_statements_{first.type}+{second.type}"


COMMENT_NODE_TYPES = {"line_comment", "block_comment"}


def comment_nodes_in_function_body(parser: Any, code: str) -> List[Any]:
    """找出函数体里面已有的注释。这里只处理 tree-sitter 能定位到的注释。"""
    body = find_function_body_block(parser, code)
    if body is None:
        return []
    return [
        n for n in walk(body)
        if n.type in COMMENT_NODE_TYPES
        and body.start_byte < n.start_byte < n.end_byte < body.end_byte
    ]


def comment_delete_range(code: str, node: Any) -> Tuple[int, int, str]:
    """整行只有注释时连同换行删除；行内注释则换成一个空格，防止前后 token 粘在一起。"""
    b = code.encode("utf-8")
    line_start = b.rfind(b"\n", 0, node.start_byte) + 1
    line_end_no_nl = b.find(b"\n", node.end_byte)
    if line_end_no_nl < 0:
        line_end_no_nl = len(b)
        line_end = line_end_no_nl
    else:
        line_end = line_end_no_nl + 1

    before = b[line_start:node.start_byte].strip()
    after = b[node.end_byte:line_end_no_nl].strip()
    if not before and not after:
        return line_start, line_end, ""
    return node.start_byte, node.end_byte, " "


def changed_comment_text(old: str) -> str:
    """保留注释形式，只改注释内容。"""
    if old.startswith("///"):
        return "/// BENCHMARK_TYPE1_COMMENT_CHANGED"
    if old.startswith("//!"):
        return "//! BENCHMARK_TYPE1_COMMENT_CHANGED"
    if old.startswith("//"):
        return "// BENCHMARK_TYPE1_COMMENT_CHANGED"
    if old.startswith("/**"):
        return "/** BENCHMARK_TYPE1_COMMENT_CHANGED */"
    if old.startswith("/*!"):
        return "/*! BENCHMARK_TYPE1_COMMENT_CHANGED */"
    return "/* BENCHMARK_TYPE1_COMMENT_CHANGED */"


def blank_line_ranges_in_function_body(parser: Any, code: str) -> List[Tuple[int, int]]:
    body = find_function_body_block(parser, code)
    if body is None:
        return []

    ranges: List[Tuple[int, int]] = []
    # Type-1 里的“删除行”只指删除空白行，不能删除普通代码行。
    for m in re.finditer(r"(?m)^[ \t]*\r?\n", code):
        start_b = len(code[:m.start()].encode("utf-8"))
        end_b = len(code[:m.end()].encode("utf-8"))
        if body.start_byte < start_b and end_b < body.end_byte:
            ranges.append((start_b, end_b))
    return ranges


def mutate_type1_comment_add(parser: Any, code: str) -> Optional[MutationResult]:
    out = block_insert_after_open(parser, code, ["// BENCHMARK_TYPE1_COMMENT_ADDED"])
    if out is None:
        return None
    mutated, pos = out
    return MutationResult(
        "type1_comment_add", "Type-1", "comment_add", mutated, pos,
        "add one comment at the beginning of the function body",
    )


def mutate_type1_comment_delete(parser: Any, code: str) -> Optional[MutationResult]:
    comments = comment_nodes_in_function_body(parser, code)
    if not comments:
        return None
    target = comments[len(comments) // 2]
    old = node_text(code, target)
    start, end, repl = comment_delete_range(code, target)
    mutated = apply_replacements_bytes(code, [(start, end, repl)])
    return MutationResult(
        "type1_comment_delete", "Type-1", "comment_delete", mutated,
        f"delete_{target.type}",
        "delete existing comment: " + normalize_snippet_for_log(old),
    )


def mutate_type1_comment_modify(parser: Any, code: str) -> Optional[MutationResult]:
    comments = comment_nodes_in_function_body(parser, code)
    if not comments:
        return None
    target = comments[len(comments) // 2]
    old = node_text(code, target)
    new = changed_comment_text(old)
    if old == new:
        return None
    mutated = apply_replacements_bytes(code, [(target.start_byte, target.end_byte, new)])
    return MutationResult(
        "type1_comment_modify", "Type-1", "comment_modify", mutated,
        f"modify_{target.type}",
        "modify existing comment text: " + normalize_snippet_for_log(old + " -> " + new),
    )


def mutate_type1_blank_line_add(parser: Any, code: str) -> Optional[MutationResult]:
    out = block_insert_after_open(parser, code, [""])
    if out is None:
        return None
    mutated, pos = out
    return MutationResult(
        "type1_blank_line_add", "Type-1", "blank_line_add", mutated, pos,
        "add one blank line in the function body",
    )


def mutate_type1_blank_line_delete(parser: Any, code: str) -> Optional[MutationResult]:
    ranges = blank_line_ranges_in_function_body(parser, code)
    if not ranges:
        return None
    start, end = ranges[len(ranges) // 2]
    mutated = apply_replacements_bytes(code, [(start, end, "")])
    return MutationResult(
        "type1_blank_line_delete", "Type-1", "blank_line_delete", mutated,
        "delete_existing_blank_line",
        "delete one existing blank line in the function body",
    )


def first_let_binding_name(parser: Any, code: str) -> Optional[str]:
    fn = find_function_node(parser, code)
    if fn is None:
        return None
    for n in walk(fn):
        if n.type == "let_declaration":
            txt = node_text(code, n)
            m = re.search(r"\blet\s+(?:mut\s+)?([A-Za-z_][A-Za-z0-9_]*)\b", txt)
            if m:
                name = m.group(1)
                if not name.startswith("_") and name not in RUST_KEYWORDS:
                    return name
    return None


def mutate_type2_identifier(parser: Any, code: str) -> Optional[MutationResult]:
    old = first_let_binding_name(parser, code)
    if not old:
        return None
    new = old + "_bench"

    fn = find_function_node(parser, code)
    if fn is None:
        return None

    reps: List[Tuple[int, int, str]] = []
    for n in walk(fn):
        if n.type in {"identifier", "shorthand_field_identifier", "field_identifier"}:
            txt = node_text(code, n)
            if txt == old:
                # Avoid changing field access `.x` if grammar reports it as identifier.
                before = code.encode("utf-8")[:n.start_byte].decode("utf-8", errors="ignore")
                if before.endswith("."):
                    continue
                reps.append((n.start_byte, n.end_byte, new))

    if not reps:
        return None

    mutated = apply_replacements_bytes(code, reps)
    return MutationResult("type2_identifier", "Type-2", "identifier_rename", mutated, "identifier_nodes", f"{old}->{new}")


def literal_replacement(kind: str, text: str) -> Optional[str]:
    if kind in {"integer_literal", "float_literal"} or "literal" in kind and re.match(r"^\d", text):
        m = re.search(r"(u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize|f32|f64)$", text)
        suffix = m.group(1) if m else ""
        return "2" + suffix if text.startswith("1") else "1" + suffix
    if kind in {"string_literal", "raw_string_literal"} or (text.startswith('"') and text.endswith('"')):
        return '"benchmark_value"'
    if kind == "char_literal":
        return "'x'"
    if kind == "boolean_literal" or text in {"true", "false"}:
        return "false" if text == "true" else "true"
    return None


def mutate_type2_literal(parser: Any, code: str) -> Optional[MutationResult]:
    fn = find_function_node(parser, code)
    if fn is None:
        return None
    literal_types = {
        "integer_literal", "float_literal", "string_literal", "raw_string_literal",
        "char_literal", "boolean_literal"
    }
    for n in walk(fn):
        txt = node_text(code, n)
        if n.type in literal_types or txt in {"true", "false"}:
            repl = literal_replacement(n.type, txt)
            if repl and repl != txt:
                mutated = apply_replacements_bytes(code, [(n.start_byte, n.end_byte, repl)])
                return MutationResult("type2_literal", "Type-2", "literal_change", mutated, f"{n.type}_node", f"{txt}->{repl}")
    return None


def normalize_snippet_for_log(text: str, limit: int = 160) -> str:
    one_line = re.sub(r"\s+", " ", text.strip())
    if len(one_line) > limit:
        return one_line[:limit] + "..."
    return one_line


# ------------------------------------------------------------
# Type-3 insertion mutations
# ------------------------------------------------------------

def mutate_type3_insert_inline(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/insert_inline:
    Small insertion inside an existing binary expression.

    Example:
      price + tax  ->  price + tax + tax

    This version deliberately does NOT use the older call-argument fallback
    `foo(x) -> foo(x, x)`, because changing call arity is hard to justify in
    Rust and may fail type checking. The inserted part reuses an existing
    operand, so no undefined identifier is introduced.
    """
    candidate = find_plus_expression_with_at_least_terms(parser, code, min_terms=2)
    if candidate is None:
        return None

    node, terms = candidate
    old = node_text(code, node).strip()
    add_term = terms[-1].strip()
    new = old + " + " + add_term
    if new == old:
        return None
    mutated = apply_replacements_bytes(code, [(node.start_byte, node.end_byte, new)])
    return MutationResult(
        "type3_insert_inline", "Type-3", "insert_inline", mutated,
        f"insert_within_{node.type}",
        "insert one existing operand within expression: " + normalize_snippet_for_log(old + " -> " + new),
    )

def mutate_type3_insert_stmt(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/insert_stmt:
    Insert exactly one parser-safe statement at an internal statement boundary.
    This light Type-3 case is kept intentionally, but boundary insertion is
    avoided when possible.
    """
    out = insert_after_internal_statement(parser, code, ["let _bench_t3_insert_stmt = 1usize;"])
    if out is None:
        return None
    mutated, pos = out
    return MutationResult("type3_insert_stmt", "Type-3", "insert_stmt", mutated, pos, "insert exactly one internal statement")


def insertion_fragment_lines(original_token_count: int) -> Tuple[List[str], float, str]:
    """Return a length-aware insertion fragment and max token-increase ratio."""
    if original_token_count < 100:
        return ([
            "let _bench_t3_frag = 1usize;",
            "let _ = _bench_t3_frag;",
        ], 0.12, "short_two_statement_fragment")
    if original_token_count < 200:
        return ([
            "if true {",
            "    let mut _bench_t3_v = 1usize;",
            "    _bench_t3_v += 1usize;",
            "    let _ = _bench_t3_v;",
            "}",
        ], 0.20, "medium_control_fragment")
    return ([
        "if true {",
        "    let mut _bench_t3_v = 1usize;",
        "    _bench_t3_v += 1usize;",
        "    let _bench_t3_w = _bench_t3_v.saturating_add(1usize);",
        "    let _ = (_bench_t3_v, _bench_t3_w);",
        "}",
    ], 0.25, "longer_control_fragment")


def mutate_type3_insert_fragment(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/insert_fragment:
    Insert a multi-line fragment at an internal statement boundary. The fragment
    length is controlled by the original function size: short functions receive
    a short fragment, while longer functions receive a larger but still bounded
    fragment. This avoids both overly tiny edits on long functions and excessive
    insertion into short functions.
    """
    original_token_count = len(raw_tokens(code))
    if original_token_count < 50:
        return None

    fragment, max_ratio, size_name = insertion_fragment_lines(original_token_count)
    out = insert_after_internal_statement(parser, code, fragment)
    if out is None:
        return None
    mutated, pos = out

    delta_tokens = len(raw_tokens(mutated)) - original_token_count
    if delta_tokens > max(1, int(original_token_count * max_ratio)):
        return None

    return MutationResult(
        "type3_insert_fragment", "Type-3", "insert_fragment", mutated, pos,
        f"insert length-aware {size_name}; token_delta={delta_tokens}; max_ratio={max_ratio}",
    )


def mutate_type3_insert_segmented(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/insert_segmented:
    Insert small statements at two separated internal positions. This represents
    copy-after-edit cases where additions are distributed rather than appearing
    as one contiguous block.
    """
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    pair = select_two_separated_statements(block, min_gap=1)
    if pair is None:
        return None
    first, second = pair
    mutated, pos = insert_after_two_statement_nodes(
        code,
        first, ["let _bench_t3_seg_a = 1usize;"],
        second, ["let _bench_t3_seg_b = _bench_t3_seg_a.saturating_add(1usize);"],
    )
    return MutationResult(
        "type3_insert_segmented", "Type-3", "insert_segmented", mutated, pos,
        "insert two small statements at separated internal positions",
    )


# ------------------------------------------------------------
# Type-3 deletion mutations
# ------------------------------------------------------------

def is_local_declaration_statement(code: str, n: Any) -> bool:
    txt = node_text(code, n).strip()
    return n.type == "let_declaration" and txt.startswith("let ") and txt.endswith(";")


def is_local_semicolon_statement(code: str, n: Any) -> bool:
    """
    General local semicolon statement candidate.
    This is parser-guided: the node comes from tree-sitter-rust, and we only use
    text checks to filter out control-flow exits, macros, items, and fallible `?`.
    """
    txt = node_text(code, n).strip()
    if not txt.endswith(";"):
        return False
    if txt.startswith("return"):
        return False
    if re.search(r"\b(break|continue)\b", txt):
        return False
    if "?" in txt:
        return False
    if "macro_rules!" in txt:
        return False
    if re.match(r"^(pub\s+)?(fn|struct|enum|impl|trait|mod)\b", txt):
        return False
    if n.type in {"let_declaration", "expression_statement"}:
        return True
    if txt.startswith("let "):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_\.\[\]\(\)]*\s*(?:=|\+=|-=|\*=|/=|%=)", txt):
        return True
    return False


def is_operation_statement(code: str, n: Any) -> bool:
    """
    Operation/action/update statement for deletion.
    Target examples:
      - assignment/update: x = expr;  x += expr;  product *= i;
      - function call: foo(x);
      - method call: obj.update();

    We avoid deleting `let` declarations unless no operation statement exists.
    """
    if not is_local_semicolon_statement(code, n):
        return False
    txt = node_text(code, n).strip()
    if txt.startswith("let ") or n.type == "let_declaration":
        return False

    # Assignment/update statements.
    if re.match(r"^[A-Za-z_][A-Za-z0-9_\.\[\]\(\)]*\s*(?:=|\+=|-=|\*=|/=|%=)", txt):
        return True

    # Function/method call statements. Avoid macro calls like println!(...).
    if "!" not in txt:
        if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*\s*\([^;]*\)\s*;$", txt, flags=re.S):
            return True
        if re.search(r"\.[A-Za-z_][A-Za-z0-9_]*\s*\([^;]*\)\s*;$", txt, flags=re.S):
            return True
    return False


def operation_deletion_candidates(code: str, block: Any) -> List[Any]:
    return [s for s in direct_statement_nodes(block) if is_operation_statement(code, s)]


def declaration_deletion_candidates(code: str, block: Any) -> List[Any]:
    return [s for s in direct_statement_nodes(block) if is_local_declaration_statement(code, s)]


def delete_byte_range_with_following_newline(code: str, start: int, end: int) -> str:
    b = code.encode("utf-8")
    if end < len(b) and b[end:end+1] == b"\n":
        end += 1
    return apply_replacements_bytes(code, [(start, end, "")])


def mutate_type3_delete_inline(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/delete_inline:
    Remove a small part inside an expression.

    Example:
      price + tax + fee  ->  price + tax

    This implements a Rust-adapted form of small deletion within a line. The edit
    is performed on a parser-located binary expression, not by deleting a physical
    source line.
    """
    candidate = find_plus_expression_with_at_least_terms(parser, code, min_terms=3)
    if candidate is None:
        # Fallback: remove one argument from a call with at least two arguments.
        call_candidate = find_call_argument_candidate(parser, code, min_args=2)
        if call_candidate is None:
            return None
        call, args_inside_start_abs, args_inside_end_abs, args = call_candidate
        # Remove the last argument, including its preceding comma when possible.
        a_start, a_end, a_txt = args[-1]
        start_abs = args_inside_start_abs + a_start
        end_abs = args_inside_start_abs + a_end
        # Include the preceding comma and whitespace if present.
        b = code.encode("utf-8")
        start = start_abs
        while start > args_inside_start_abs and b[start-1:start] in {b" ", b"\t", b"\n", b"\r"}:
            start -= 1
        if start > args_inside_start_abs and b[start-1:start] == b",":
            start -= 1
        mutated = apply_replacements_bytes(code, [(start, end_abs, "")])
        return MutationResult(
            "type3_delete_inline", "Type-3", "delete_inline", mutated,
            f"delete_argument_in_{call.type}",
            "delete one existing argument from call argument list: " + normalize_snippet_for_log(a_txt),
        )

    node, terms = candidate
    old = node_text(code, node).strip()
    new = " + ".join(terms[:-1]).strip()
    if not new or new == old:
        return None
    mutated = apply_replacements_bytes(code, [(node.start_byte, node.end_byte, new)])
    return MutationResult(
        "type3_delete_inline", "Type-3", "delete_inline", mutated,
        f"delete_within_{node.type}",
        "delete one operand from expression: " + normalize_snippet_for_log(old + " -> " + new),
    )


def mutate_type3_delete_stmt(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/delete_stmt:
    Delete one parser-selected statement node. Prefer operation/action/update
    statements. If none exist, fall back to one local declaration rather than
    forcing an unsafe text-level edit.
    """
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    candidates = operation_deletion_candidates(code, block)
    fallback = False
    if not candidates:
        candidates = declaration_deletion_candidates(code, block)
        fallback = True
    if not candidates:
        return None
    target = candidates[len(candidates) // 2]
    deleted = node_text(code, target)
    mutated = delete_byte_range_with_following_newline(code, target.start_byte, target.end_byte)
    kind = "declaration_fallback" if fallback else "operation"
    return MutationResult(
        "type3_delete_stmt", "Type-3", "delete_stmt", mutated,
        f"delete_{target.type}_{kind}",
        f"delete one {kind} statement: " + normalize_snippet_for_log(deleted),
    )


def mutate_type3_delete_fragment(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/delete_fragment:
    Delete two consecutive operation/action/update statements in the same block.
    If no such pair exists, skip this mutation. Declarations are not used to pad
    this rule.
    """
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    stmts = direct_statement_nodes(block)
    op_ids = {id(s) for s in stmts if is_operation_statement(code, s)}
    for i in range(0, len(stmts) - 1):
        a, b = stmts[i], stmts[i + 1]
        if id(a) in op_ids and id(b) in op_ids:
            deleted = node_text(code, a) + "\n" + node_text(code, b)
            mutated = delete_byte_range_with_following_newline(code, a.start_byte, b.end_byte)
            return MutationResult(
                "type3_delete_fragment", "Type-3", "delete_fragment", mutated,
                f"delete_{a.type}+{b.type}_operations",
                "delete two consecutive operation/action statements: " + normalize_snippet_for_log(deleted),
            )
    return None


def deletion_range_with_following_newline(code: str, start: int, end: int) -> Tuple[int, int, str]:
    b = code.encode("utf-8")
    if end < len(b) and b[end:end+1] == b"\n":
        end += 1
    return (start, end, "")


def mutate_type3_delete_segmented(parser: Any, code: str) -> Optional[MutationResult]:
    """
    Type-3/delete_segmented:
    For longer functions only, delete two non-adjacent operation/action/update
    statements from separated positions. This rule is intentionally restricted
    to >=150-token functions to avoid over-deleting short functions.
    """
    if len(raw_tokens(code)) < 150:
        return None
    block = find_function_body_block(parser, code)
    if block is None:
        return None
    stmts = direct_statement_nodes(block)
    ops: List[Tuple[int, Any]] = [(i, s) for i, s in enumerate(stmts) if is_operation_statement(code, s)]
    if len(ops) < 2:
        return None
    best: Optional[Tuple[int, Any, int, Any]] = None
    # Prefer a pair from separated regions, not adjacent operations.
    for i, a in ops:
        for j, b in ops:
            if j <= i + 1:
                continue
            score = abs((i / max(1, len(stmts)-1)) - 0.33) + abs((j / max(1, len(stmts)-1)) - 0.67)
            if best is None:
                best = (i, a, j, b)
                best_score = score
            elif score < best_score:  # type: ignore[name-defined]
                best = (i, a, j, b)
                best_score = score
    if best is None:
        return None
    _, a, _, b = best
    deleted = node_text(code, a) + " ... " + node_text(code, b)
    reps = [
        deletion_range_with_following_newline(code, a.start_byte, a.end_byte),
        deletion_range_with_following_newline(code, b.start_byte, b.end_byte),
    ]
    mutated = apply_replacements_bytes(code, reps)
    return MutationResult(
        "type3_delete_segmented", "Type-3", "delete_segmented", mutated,
        f"delete_two_non_adjacent_{a.type}+{b.type}",
        "delete two separated operation/action statements: " + normalize_snippet_for_log(deleted),
    )


# ------------------------------------------------------------
# Type-3 modification mutation
# ------------------------------------------------------------

def split_top_level(text: str, sep: str = "+") -> Optional[List[str]]:
    """Split text by a top-level single-character separator."""
    parts: List[str] = []
    depth = 0
    quote: Optional[str] = None
    escape = False
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in {'"', "'"}:
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    parts.append(text[start:].strip())
    parts = [p for p in parts if p]
    return parts if len(parts) >= 2 else None


def is_simple_value_expr(expr: str) -> bool:
    expr = expr.strip()
    # Simple identifiers, field accesses, paths, and integer literals are okay.
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::)[A-Za-z_][A-Za-z0-9_]*)*$", expr):
        return True
    if re.match(r"^\d[\d_]*(?:u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize)?$", expr):
        return True
    return False


def find_plus_expression_with_at_least_terms(parser: Any, code: str, min_terms: int) -> Optional[Tuple[Any, List[str]]]:
    fn = find_function_node(parser, code)
    if fn is None:
        return None
    candidates: List[Tuple[Any, List[str]]] = []
    for n in walk(fn):
        if n.type != "binary_expression":
            continue
        txt = node_text(code, n).strip()
        if "+" not in txt:
            continue
        terms = split_top_level(txt, "+")
        if terms is None or len(terms) < min_terms:
            continue
        # Prefer simple operand chains such as price + tax + fee.
        if all(is_simple_value_expr(t) for t in terms):
            candidates.append((n, terms))
    if not candidates:
        return None
    return candidates[len(candidates) // 2]


def top_level_split_args(arg_text: str) -> List[Tuple[int, int, str]]:
    """
    Split a Rust call argument list at top-level commas.
    Returns (start_index, end_index, argument_text) within arg_text.
    """
    out: List[Tuple[int, int, str]] = []
    depth = 0
    start = 0
    quote: Optional[str] = None
    escape = False
    i = 0
    while i < len(arg_text):
        ch = arg_text[i]
        if quote is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in {'"', "'"}:
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            raw = arg_text[start:i]
            stripped = raw.strip()
            if stripped:
                left_ws = len(raw) - len(raw.lstrip())
                right_ws = len(raw.rstrip())
                out.append((start + left_ws, start + right_ws, stripped))
            start = i + 1
        i += 1
    raw = arg_text[start:]
    stripped = raw.strip()
    if stripped:
        left_ws = len(raw) - len(raw.lstrip())
        right_ws = len(raw.rstrip())
        out.append((start + left_ws, start + right_ws, stripped))
    return out


def find_call_argument_candidate(parser: Any, code: str, min_args: int = 1) -> Optional[Tuple[Any, int, int, List[Tuple[int, int, str]]]]:
    fn = find_function_node(parser, code)
    if fn is None:
        return None
    candidates: List[Tuple[Any, int, int, List[Tuple[int, int, str]]]] = []
    for n in walk(fn):
        if n.type not in {"call_expression", "method_call_expression"}:
            continue
        txt = node_text(code, n)
        head = txt[:txt.find("(")] if "(" in txt else txt
        if "!" in head:
            continue
        open_pos = txt.find("(")
        close_pos = txt.rfind(")")
        if open_pos < 0 or close_pos <= open_pos:
            continue
        args_inside = txt[open_pos + 1:close_pos]
        args = top_level_split_args(args_inside)
        if len(args) < min_args:
            continue
        args_start_abs = n.start_byte + len(txt[:open_pos + 1].encode("utf-8"))
        args_end_abs = n.start_byte + len(txt[:close_pos].encode("utf-8"))
        candidates.append((n, args_start_abs, args_end_abs, args))
    if not candidates:
        return None
    return candidates[len(candidates) // 2]


def has_ancestor_type(node: Any, types: Sequence[str]) -> bool:
    wanted = set(types)
    cur = getattr(node, "parent", None)
    while cur is not None:
        if getattr(cur, "type", None) in wanted:
            return True
        cur = getattr(cur, "parent", None)
    return False


def find_top_level_single_char_operator(text: str, op: str) -> Optional[int]:
    """Find a top-level single-character operator, excluding <=, >=, <<, >>."""
    depth = 0
    quote: Optional[str] = None
    escape = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in {'"', "'"}:
            quote = ch
        elif ch in "([{" :
            depth += 1
        elif ch in ")]}" :
            depth = max(0, depth - 1)
        elif ch == op and depth == 0:
            prev_ch = text[i - 1] if i > 0 else ""
            next_ch = text[i + 1] if i + 1 < len(text) else ""
            if op == "<" and next_ch not in {"=", "<"} and prev_ch != "<":
                return i
            if op == ">" and next_ch != ">" and prev_ch not in {"=", ">"}:
                return i
        i += 1
    return None


def char_index_to_byte_offset(text: str, char_index: int) -> int:
    """Convert a character index in a Python string to a UTF-8 byte offset."""
    return len(text[:char_index].encode("utf-8"))


def first_standalone_identifier(expr: str) -> Optional[str]:
    """
    Select a simple identifier that can be used in a guard expression.
    Field names such as `.len` or path components such as `foo::bar` are skipped.
    """
    for m in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr):
        name = m.group(0)
        if name in RUST_KEYWORDS:
            continue
        prev_ch = expr[m.start() - 1] if m.start() > 0 else ""
        next_ch = expr[m.end()] if m.end() < len(expr) else ""
        if prev_ch in {".", ":"} or next_ch == ":":
            continue
        return name
    return None


def is_rhs_candidate_for_if_replacement(rhs: str) -> bool:
    """
    Conservative textual filter for numeric-looking RHS expressions.
    The mutation is parser-validated later, but this filter avoids obviously
    unnatural replacements such as strings, booleans, macros, and existing blocks.
    """
    r = rhs.strip()
    if not r:
        return False
    if "?" in r or "!" in r:
        return False
    if "{" in r or "}" in r:
        return False
    if re.search(r"\b(true|false)\b", r):
        return False
    if re.search(r"[\"']", r):
        return False
    if re.match(r"^(if|match|loop|while|for|return|break|continue)\b", r):
        return False
    # Prefer arithmetic / numeric expressions such as `n * 10` or `total + fee`.
    if not re.search(r"[+\-*/%]|\b\d", r):
        return False
    return first_standalone_identifier(r) is not None


def statement_rhs_span_for_modify(stmt_text: str) -> Optional[Tuple[int, int, str, str]]:
    """
    Locate the RHS of a simple `let` declaration or assignment statement.
    Returns (rhs_start_char, rhs_end_char, rhs_text, statement_kind).
    """
    patterns = [
        (
            "let_rhs",
            re.compile(
                r"^(?P<prefix>\s*let\s+(?:mut\s+)?[A-Za-z_][A-Za-z0-9_]*(?:\s*:\s*[^=;]+)?\s*=\s*)"
                r"(?P<rhs>.+?)"
                r"(?P<suffix>\s*;)\s*$",
                re.S,
            ),
        ),
        (
            "assign_rhs",
            re.compile(
                r"^(?P<prefix>\s*[A-Za-z_][A-Za-z0-9_]*(?:\s*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]]+\]))?\s*=\s*)"
                r"(?P<rhs>.+?)"
                r"(?P<suffix>\s*;)\s*$",
                re.S,
            ),
        ),
    ]
    for kind, pat in patterns:
        m = pat.match(stmt_text)
        if not m:
            continue
        rhs = m.group("rhs").strip()
        if not is_rhs_candidate_for_if_replacement(rhs):
            continue
        return m.start("rhs"), m.end("rhs"), rhs, kind
    return None


def modify_let_rhs_if_expression(parser: Any, code: str) -> Optional[MutationResult]:
    """
    行内变更的第1种：只改 let 文右边的表达式。

    例：
      let fee = n * 10;
        ->
      let fee = if n > 0 { n * 10 } else { 0 };

    这里不删除或增加一个完整的文，只替换原 let 文内部的 RHS。
    """
    fn = find_function_node(parser, code)
    if fn is None:
        return None

    candidates: List[Tuple[Any, int, int, str, str]] = []
    for n in walk(fn):
        if n.type != "let_declaration":
            continue
        stmt_text = node_text(code, n)
        span = statement_rhs_span_for_modify(stmt_text)
        if span is None:
            continue
        rhs_start_ch, rhs_end_ch, rhs, stmt_kind = span
        if stmt_kind != "let_rhs":
            continue
        guard = first_standalone_identifier(rhs)
        if guard is None:
            continue
        new_rhs = f"if {guard} > 0 {{ {rhs} }} else {{ 0 }}"
        if new_rhs == rhs:
            continue
        start_abs = n.start_byte + char_index_to_byte_offset(stmt_text, rhs_start_ch)
        end_abs = n.start_byte + char_index_to_byte_offset(stmt_text, rhs_end_ch)
        candidates.append((n, start_abs, end_abs, rhs, new_rhs))

    if not candidates:
        return None

    target, start_abs, end_abs, old_rhs, new_rhs = candidates[len(candidates) // 2]
    mutated = apply_replacements_bytes(code, [(start_abs, end_abs, new_rhs)])
    return MutationResult(
        "type3_modify_inline_let_if", "Type-3", "modify_inline", mutated,
        f"modify_{target.type}_rhs",
        "replace let RHS with if-expression: "
        + normalize_snippet_for_log(old_rhs + " -> " + new_rhs),
    )


def modify_comparison_operator(parser: Any, code: str) -> Optional[MutationResult]:
    """
    行内变更的第2种：改变一个比较运算符，例如 < -> <=、> -> >=。

    为了避免明显影响循环条件，这里不选择 while/loop 里面的比较式。
    """
    fn = find_function_node(parser, code)
    if fn is None:
        return None

    candidates: List[Tuple[Any, str, str]] = []
    for n in walk(fn):
        if n.type != "binary_expression":
            continue
        if has_ancestor_type(n, {"while_expression", "loop_expression"}):
            continue
        txt = node_text(code, n).strip()
        replacement: Optional[str] = None
        pos = find_top_level_single_char_operator(txt, "<")
        if pos is not None:
            replacement = txt[:pos] + "<=" + txt[pos + 1:]
        else:
            pos = find_top_level_single_char_operator(txt, ">")
            if pos is not None:
                replacement = txt[:pos] + ">=" + txt[pos + 1:]
        if replacement and replacement != txt:
            candidates.append((n, txt, replacement))

    if not candidates:
        return None
    target, old, new = candidates[len(candidates) // 2]
    mutated = apply_replacements_bytes(code, [(target.start_byte, target.end_byte, new)])
    return MutationResult(
        "type3_modify_inline_compare", "Type-3", "modify_inline", mutated,
        f"modify_{target.type}_comparison_operator",
        "replace comparison operator: " + normalize_snippet_for_log(old + " -> " + new),
    )


def replace_top_level_arithmetic_operator(expr: str) -> Optional[str]:
    """把 RHS 中最外层的一个计算运算符换成另一个运算符。"""
    # 为了不把负号当成减法，从左到右找时跳过一元 +/-。
    replacements = {"+": "-", "-": "+", "*": "/", "/": "*", "%": "+"}
    depth = 0
    quote: Optional[str] = None
    escape = False

    for i, ch in enumerate(expr):
        if quote is not None:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue

        if ch in {'"', "'"}:
            quote = ch
            continue
        if ch in "([{":
            depth += 1
            continue
        if ch in ")]}":
            depth = max(0, depth - 1)
            continue
        if depth != 0 or ch not in replacements:
            continue

        # 开头或前一个字符也是运算符时，多半是一元 +/-，先跳过。
        before = expr[:i].rstrip()
        if ch in {"+", "-"} and (not before or before[-1] in "=([{,+-*/%<>!&|"):
            continue

        return expr[:i] + replacements[ch] + expr[i + 1:]
    return None


def parse_whole_modifiable_statement(stmt_text: str) -> Optional[Dict[str, str]]:
    """
    找出可以作为“一文/复数文/分散变更”候选的 let 文或赋值文。
    返回的信息之后用于重建整个 statement，而不是只替换一个 byte 范围。
    """
    let_pat = re.compile(
        r"^(?P<indent>\s*)(?P<head>let\s+(?:mut\s+)?[A-Za-z_][A-Za-z0-9_]*(?:\s*:\s*[^=;]+)?\s*=\s*)"
        r"(?P<rhs>.+?)(?P<tail>\s*;)\s*$",
        re.S,
    )
    assign_pat = re.compile(
        r"^(?P<indent>\s*)(?P<lhs>[A-Za-z_][A-Za-z0-9_]*(?:\s*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]]+\]))*)"
        r"(?P<space1>\s*)(?P<op>\+=|-=|\*=|/=|%=|=)(?P<space2>\s*)"
        r"(?P<rhs>.+?)(?P<tail>\s*;)\s*$",
        re.S,
    )

    m = let_pat.match(stmt_text)
    if m:
        rhs = m.group("rhs").strip()
        if rhs and "?" not in rhs and "macro_rules!" not in rhs:
            return {
                "kind": "let",
                "indent": m.group("indent"),
                "head": m.group("head"),
                "rhs": rhs,
                "tail": m.group("tail"),
            }

    m = assign_pat.match(stmt_text)
    if m:
        rhs = m.group("rhs").strip()
        if rhs and "?" not in rhs and "macro_rules!" not in rhs:
            return {
                "kind": "assignment",
                "indent": m.group("indent"),
                "lhs": m.group("lhs"),
                "space1": m.group("space1"),
                "op": m.group("op"),
                "space2": m.group("space2"),
                "rhs": rhs,
                "tail": m.group("tail"),
            }
    return None


def build_whole_statement_replacement(stmt_text: str, prefer_if_for_let: bool) -> Optional[Tuple[str, str]]:
    """
    生成一个完整 statement 的替换结果。

    let 文：优先把 RHS 换成 if 表达式，或者改变 RHS 中的计算运算符。
    赋值文：复合赋值改变赋值运算符；普通赋值改变 RHS 中的计算式。
    """
    info = parse_whole_modifiable_statement(stmt_text)
    if info is None:
        return None

    if info["kind"] == "let":
        rhs = info["rhs"]
        new_rhs: Optional[str] = None

        if prefer_if_for_let and is_rhs_candidate_for_if_replacement(rhs):
            guard = first_standalone_identifier(rhs)
            if guard is not None:
                new_rhs = f"if {guard} > 0 {{ {rhs} }} else {{ 0 }}"

        if new_rhs is None:
            new_rhs = replace_top_level_arithmetic_operator(rhs)

        # 没有计算运算符时，最后再尝试 if 表达式。
        if new_rhs is None and is_rhs_candidate_for_if_replacement(rhs):
            guard = first_standalone_identifier(rhs)
            if guard is not None:
                new_rhs = f"if {guard} > 0 {{ {rhs} }} else {{ 0 }}"

        if new_rhs is None or new_rhs == rhs:
            return None
        new_stmt = info["indent"] + info["head"] + new_rhs + info["tail"]
        return new_stmt, "let_statement"

    op = info["op"]
    rhs = info["rhs"]
    new_op = {"+=": "-=", "-=": "+=", "*=": "/=", "/=": "*=", "%=": "+="}.get(op)
    new_rhs: Optional[str] = None

    if new_op is None:
        # 普通 = 赋值时，改变 RHS 内部的计算式。
        new_op = op
        new_rhs = replace_top_level_arithmetic_operator(rhs)
        if new_rhs is None and is_rhs_candidate_for_if_replacement(rhs):
            guard = first_standalone_identifier(rhs)
            if guard is not None:
                new_rhs = f"if {guard} > 0 {{ {rhs} }} else {{ 0 }}"
    else:
        # += -> -= 这类变更已经足够，所以 RHS 保持原样。
        new_rhs = rhs

    if new_rhs is None:
        return None
    new_stmt = (
        info["indent"] + info["lhs"] + info["space1"]
        + new_op + info["space2"] + new_rhs + info["tail"]
    )
    if new_stmt.strip() == stmt_text.strip():
        return None
    return new_stmt, "assignment_statement"


def whole_statement_candidates(parser: Any, code: str, prefer_if_for_let: bool) -> List[Tuple[int, Any, str, str, str]]:
    """返回 direct statement 中可以完整替换的候选。"""
    block = find_function_body_block(parser, code)
    if block is None:
        return []

    out: List[Tuple[int, Any, str, str, str]] = []
    for index, node in enumerate(direct_statement_nodes(block)):
        if node.type not in {"let_declaration", "expression_statement"}:
            continue
        old_stmt = node_text(code, node)
        replacement = build_whole_statement_replacement(old_stmt, prefer_if_for_let)
        if replacement is None:
            continue
        new_stmt, kind = replacement
        out.append((index, node, old_stmt, new_stmt, kind))
    return out


def mutate_type3_modify_inline(parser: Any, code: str) -> Optional[MutationResult]:
    """
    行内变更包含两种形式：
      1. let 文 RHS -> if 表达式
      2. < -> <= 或 > -> >=

    每个 seed 在 modify_inline 类中只生成一个变体。优先使用原来已有的
    let-RHS-to-if 规则；没有符合条件的 let 文时，再使用比较运算符变更。
    """
    let_if = modify_let_rhs_if_expression(parser, code)
    if let_if is not None:
        return let_if
    return modify_comparison_operator(parser, code)


def make_modify_stmt_local_name(code: str) -> str:
    """为一文变更生成一个尽量不和原函数冲突的局部变量名。"""
    func_name = safe_id(extract_function_name(code)).lower()
    base = f"_bench_t3_stmt_{func_name}"
    name = base
    index = 2

    # 如果原代码里已经有同名标识符，就在后面加数字。
    while re.search(rf"\b{re.escape(name)}\b", code):
        name = f"{base}_{index}"
        index += 1
    return name


def whole_replace_stmt_candidates(parser: Any, code: str) -> List[Tuple[Any, str, str]]:
    """
    找出适合“一文完全变更”的赋值/更新文。

    例如：
      total = total + fee;
      total += fee;

    这里不选择 let 声明。因为删除原来的 let 声明后，后面的代码可能还会
    使用该变量，虽然 parser 可以通过，但会很容易生成明显无法编译的代码。
    找不到赋值/更新文时就跳过本规则。
    """
    block = find_function_body_block(parser, code)
    if block is None:
        return []

    local_name = make_modify_stmt_local_name(code)
    new_stmt = f"let {local_name} = 1usize;"
    candidates: List[Tuple[Any, str, str]] = []

    for node in direct_statement_nodes(block):
        if node.type != "expression_statement":
            continue

        old_stmt = node_text(code, node)
        info = parse_whole_modifiable_statement(old_stmt)
        if info is None or info["kind"] != "assignment":
            continue

        candidates.append((node, old_stmt, new_stmt))

    return candidates


def mutate_type3_modify_stmt(parser: Any, code: str) -> Optional[MutationResult]:
    """
    一文变更：把一条原有的赋值/更新文完整替换成另一条独立的完整文。

    例：
      total = total + fee;
        ->
      let _bench_t3_stmt_calculate = 1usize;

    这和 modify_inline 不同：这里替换的是整个 statement 节点，而不是只改
    statement 内部的 RHS 或运算符。没有合适候选时直接跳过。
    """
    candidates = whole_replace_stmt_candidates(parser, code)
    if not candidates:
        return None

    target, old_stmt, new_stmt = candidates[len(candidates) // 2]
    mutated = apply_replacements_bytes(code, [(target.start_byte, target.end_byte, new_stmt)])
    return MutationResult(
        "type3_modify_stmt", "Type-3", "modify_stmt", mutated,
        f"replace_whole_{target.type}_with_local_declaration",
        "replace one complete assignment/update statement: "
        + normalize_snippet_for_log(old_stmt + " -> " + new_stmt),
    )


def mutate_type3_modify_fragment(parser: Any, code: str) -> Optional[MutationResult]:
    """
    连续复数文变更：替换两个连续的 let/赋值文。
    可以是 let+let、赋值+赋值，也可以是 let+赋值。找不到就跳过。
    """
    candidates = whole_statement_candidates(parser, code, prefer_if_for_let=True)
    if len(candidates) < 2:
        return None

    pair: Optional[Tuple[Tuple[int, Any, str, str, str], Tuple[int, Any, str, str, str]]] = None
    for a, b in zip(candidates, candidates[1:]):
        if b[0] == a[0] + 1:
            pair = (a, b)
            break
    if pair is None:
        return None

    a, b = pair
    mutated = apply_replacements_bytes(code, [
        (a[1].start_byte, a[1].end_byte, a[3]),
        (b[1].start_byte, b[1].end_byte, b[3]),
    ])
    detail = (
        "replace two consecutive statements: "
        + normalize_snippet_for_log(a[2]) + " | "
        + normalize_snippet_for_log(b[2])
    )
    return MutationResult(
        "type3_modify_fragment", "Type-3", "modify_fragment", mutated,
        f"replace_consecutive_{a[1].type}+{b[1].type}", detail,
    )


def mutate_type3_modify_segmented(parser: Any, code: str) -> Optional[MutationResult]:
    """
    分散变更：替换两个不相邻的 let/赋值文，中间至少保留一个原 statement。
    选择方法和分散删除类似；不存在符合条件的两个位置时直接跳过。
    """
    candidates = whole_statement_candidates(parser, code, prefer_if_for_let=True)
    if len(candidates) < 2:
        return None

    pair: Optional[Tuple[Tuple[int, Any, str, str, str], Tuple[int, Any, str, str, str]]] = None
    best_gap = -1
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            gap = candidates[j][0] - candidates[i][0]
            if gap >= 2 and gap > best_gap:
                pair = (candidates[i], candidates[j])
                best_gap = gap
    if pair is None:
        return None

    a, b = pair
    mutated = apply_replacements_bytes(code, [
        (a[1].start_byte, a[1].end_byte, a[3]),
        (b[1].start_byte, b[1].end_byte, b[3]),
    ])
    detail = (
        "replace two separated statements: "
        + normalize_snippet_for_log(a[2]) + " ... "
        + normalize_snippet_for_log(b[2])
    )
    return MutationResult(
        "type3_modify_segmented", "Type-3", "modify_segmented", mutated,
        f"replace_non_adjacent_{a[1].type}+{b[1].type}", detail,
    )


MUTATORS = [
    # Type-1：增加之外，也加入删除和修改。
    mutate_type1_comment_add,
    mutate_type1_comment_delete,
    mutate_type1_comment_modify,
    mutate_type1_blank_line_add,
    mutate_type1_blank_line_delete,
    mutate_type2_identifier,
    mutate_type2_literal,

    # Type-3：追加4类、删除4类、变更4类，一共12类。
    mutate_type3_insert_inline,
    mutate_type3_insert_stmt,
    mutate_type3_insert_fragment,
    mutate_type3_insert_segmented,
    mutate_type3_delete_inline,
    mutate_type3_delete_stmt,
    mutate_type3_delete_fragment,
    mutate_type3_delete_segmented,
    mutate_type3_modify_inline,
    mutate_type3_modify_stmt,
    mutate_type3_modify_fragment,
    mutate_type3_modify_segmented,
]


# ============================================================
# rustfmt optional validation
# ============================================================

def rustfmt_available() -> bool:
    return shutil.which("rustfmt") is not None


def rustfmt_check_text(code: str, timeout_sec: int = 10) -> Tuple[bool, str]:
    if not rustfmt_available():
        return False, "rustfmt_not_found"
    try:
        proc = subprocess.run(
            ["rustfmt", "--emit", "stdout"],
            input=code.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
        )
        if proc.returncode == 0:
            return True, "ok"
        return False, proc.stderr.decode("utf-8", errors="replace")[:500]
    except Exception as e:
        return False, str(e)[:500]


# ============================================================
# Pair helpers
# ============================================================

def add_pair(
    rows: List[Dict[str, Any]],
    counters: Dict[str, int],
    left_id: str,
    right_id: str,
    label: int,
    pair_type: str,
    mutation_rule: str,
    mutation_subtype: str,
    negative_type: str,
    token_ov: float,
    line_sr: float,
    run_ratio: float,
    note: str,
    metric_basis: str = "normalized",
    length_ratio_value: Optional[float] = None,
) -> None:
    prefix = "P_POS" if label == 1 else "P_NEG"
    counters[prefix] = counters.get(prefix, 0) + 1
    idx = counters[prefix]
    rows.append({
        "pair_id": f"{prefix}_{idx:06d}",
        "left_id": left_id,
        "right_id": right_id,
        "left_file": f"src/{left_id}.rs",
        "right_file": f"src/{right_id}.rs",
        "label": label,
        "pair_type": pair_type,
        "mutation_rule": mutation_rule,
        "mutation_subtype": mutation_subtype,
        "negative_type": negative_type,
        "token_overlap": f"{token_ov:.4f}",
        "line_sr": f"{line_sr:.4f}",
        "longest_common_token_run_ratio": f"{run_ratio:.4f}",
        "metric_basis": metric_basis,
        "length_ratio": "" if length_ratio_value is None else f"{length_ratio_value:.4f}",
        "note": note,
    })


def compute_pair_metrics(code_a: str, code_b: str) -> Tuple[float, float, float]:
    toks_a = norm_tokens(code_a)
    toks_b = norm_tokens(code_b)
    ov = multiset_overlap_max(toks_a, toks_b)
    sr = line_sr_max(norm_line_units(code_a), norm_line_units(code_b), n=3)
    rr = run_ratio_min(toks_a, toks_b)
    return ov, sr, rr


# ============================================================
# Main benchmark generation
# ============================================================

def build_benchmark(args: argparse.Namespace) -> None:
    parser = load_rust_parser()
    rng = random.Random(args.random_seed)

    out_dir = Path(args.out_dir)
    bench_src = out_dir / "benchmark_project" / "src"
    bench_src.mkdir(parents=True, exist_ok=True)

    projects = load_projects(args)
    log(f"Projects: {len(projects)}")

    candidates: List[Dict[str, Any]] = []
    seed_rejections: List[Dict[str, Any]] = []
    filter_counts: Dict[str, int] = {}

    for pidx, p in enumerate(projects, start=1):
        pname = p["project_name"]
        proot = Path(p["project_root"])
        fjsonl = Path(p["functions_jsonl"])
        log(f"Loading project {pidx}/{len(projects)}: {pname}")
        if not fjsonl.exists():
            log(f"[WARN] missing functions_jsonl: {fjsonl}")
            continue

        for row_idx, row in enumerate(read_jsonl(fjsonl), start=1):
            code = get_code_from_row(proot, row)
            if not code or not code.strip():
                reason = "missing_code"
                filter_counts[reason] = filter_counts.get(reason, 0) + 1
                continue

            ok, reason = seed_filter(parser, code, args)
            if not ok:
                filter_counts[reason] = filter_counts.get(reason, 0) + 1
                seed_rejections.append({
                    "project_name": pname,
                    "row_idx": row_idx,
                    "func_id": row.get("func_id", ""),
                    "file": row.get("file", ""),
                    "reason": reason,
                })
                continue

            rtoks = raw_tokens(code)
            rlines = raw_line_units(code)
            cand = dict(row)
            cand.update({
                "code": code,
                "project_name": pname,
                "project_index": pidx,
                "project_root": str(proot),
                "functions_jsonl": str(fjsonl),
                "source_func_global_id": f"{pname}::{row.get('func_id', row_idx)}",
                "function_name": extract_function_name(code),
                "raw_tokens_for_benchmark": rtoks,
                "raw_lines_for_benchmark": rlines,
                "raw_token_count": len(rtoks),
                "raw_line_count": len(rlines),
            })
            candidates.append(cand)

    log(f"Candidates before exact de-duplication: {len(candidates)}")
    candidates, exact_rejections = exact_deduplicate(candidates)
    seed_rejections.extend(exact_rejections)
    log(f"Candidates after exact de-duplication: {len(candidates)}")

    if not candidates:
        raise RuntimeError("No usable seed functions. Relax filters or check input extraction.")

    rng.shuffle(candidates)
    seeds = candidates if args.num_seeds <= 0 else candidates[:args.num_seeds]
    log(f"Seeds to use: {len(seeds)}")

    selected_seeds_rows: List[Dict[str, Any]] = []
    functions_out: List[Dict[str, Any]] = []
    pairs_out: List[Dict[str, Any]] = []
    mutation_log: List[Dict[str, Any]] = []
    pair_counters: Dict[str, int] = {}

    # bench id -> code/metadata for negative sampling
    function_by_id: Dict[str, Dict[str, Any]] = {}

    positive_count = 0
    for idx, seed in enumerate(seeds, start=1):
        if args.progress_every > 0 and (idx == 1 or idx % args.progress_every == 0 or idx == len(seeds)):
            log(f"Seed {idx}/{len(seeds)} | functions={len(functions_out)} | positives={positive_count}")

        sid = f"P{int(seed['project_index']):03d}_S{idx:05d}"
        original_id = f"{sid}_original"
        original_file = f"{original_id}.rs"
        original_code = seed["code"]

        write_text(bench_src / original_file, wrap_as_rust_file(original_code, original_id))

        seed_row = {
            "bench_func_id": original_id,
            "seed_family_id": original_id,
            "project_name": seed["project_name"],
            "source_func_id": seed.get("func_id", ""),
            "source_func_global_id": seed.get("source_func_global_id", ""),
            "function_name": seed.get("function_name", ""),
            "variant": "original",
            "mutation_rule": "none",
            "mutation_subtype": "none",
            "clone_type": "seed",
            "language": "rust",
            "file": str(Path("src") / original_file).replace("\\", "/"),
            "source_file": seed.get("file", ""),
            "source_start_line": seed.get("start_line"),
            "source_end_line": seed.get("end_line"),
            "raw_token_count": seed.get("raw_token_count", 0),
            "raw_line_count": seed.get("raw_line_count", 0),
            "parse_ok": True,
            "rustfmt_ok": "",
            "code": original_code,
        }
        functions_out.append(seed_row)
        function_by_id[original_id] = seed_row
        selected_seeds_rows.append(seed_row)

        family_variant_ids = [original_id]
        seen_variant_sigs = {code_signature(original_code)}

        for mutator in MUTATORS:
            mr = mutator(parser, original_code)
            if mr is None:
                continue
            if mr.code == original_code:
                continue
            if code_signature(mr.code) in seen_variant_sigs:
                continue

            parse_valid = validate_mutation(parser, mr.code)
            if not parse_valid:
                mutation_log.append({
                    "seed_id": original_id,
                    "rule_name": mr.rule_name,
                    "clone_type": mr.clone_type,
                    "mutation_subtype": mr.subtype,
                    "accepted": False,
                    "reject_reason": "parser_validation_failed",
                    "position": mr.position,
                    "detail": mr.detail,
                })
                continue

            rustfmt_ok = ""
            rustfmt_msg = ""
            if args.rustfmt_check or args.require_rustfmt:
                ok_fmt, msg = rustfmt_check_text(wrap_as_rust_file(mr.code, "fmt_check"))
                rustfmt_ok = str(ok_fmt)
                rustfmt_msg = msg
                if not ok_fmt and args.require_rustfmt:
                    mutation_log.append({
                        "seed_id": original_id,
                        "rule_name": mr.rule_name,
                        "clone_type": mr.clone_type,
                        "mutation_subtype": mr.subtype,
                        "accepted": False,
                        "reject_reason": "rustfmt_failed",
                        "position": mr.position,
                        "detail": rustfmt_msg,
                    })
                    continue

            ov, sr, rr = compute_pair_metrics(original_code, mr.code)
            lr = length_ratio(len(norm_tokens(original_code)), len(norm_tokens(mr.code)))

            if lr > args.positive_length_ratio_max:
                mutation_log.append({
                    "seed_id": original_id,
                    "rule_name": mr.rule_name,
                    "clone_type": mr.clone_type,
                    "mutation_subtype": mr.subtype,
                    "accepted": False,
                    "reject_reason": f"positive_length_ratio>{args.positive_length_ratio_max}",
                    "length_ratio": f"{lr:.4f}",
                    "position": mr.position,
                    "detail": mr.detail,
                })
                continue

            mid = f"{sid}_{mr.rule_name}"
            mfile = f"{mid}.rs"
            write_text(bench_src / mfile, wrap_as_rust_file(mr.code, mid))

            frow = {
                "bench_func_id": mid,
                "seed_family_id": original_id,
                "project_name": seed["project_name"],
                "source_func_id": seed.get("func_id", ""),
                "source_func_global_id": seed.get("source_func_global_id", ""),
                "function_name": seed.get("function_name", ""),
                "variant": "mutated",
                "mutation_rule": mr.rule_name,
                "mutation_subtype": mr.subtype,
                "clone_type": mr.clone_type,
                "language": "rust",
                "file": str(Path("src") / mfile).replace("\\", "/"),
                "source_file": seed.get("file", ""),
                "source_start_line": seed.get("start_line"),
                "source_end_line": seed.get("end_line"),
                "raw_token_count": len(raw_tokens(mr.code)),
                "raw_line_count": len(raw_line_units(mr.code)),
                "parse_ok": True,
                "rustfmt_ok": rustfmt_ok,
                "code": mr.code,
            }
            functions_out.append(frow)
            function_by_id[mid] = frow
            family_variant_ids.append(mid)
            seen_variant_sigs.add(code_signature(mr.code))

            add_pair(
                pairs_out,
                pair_counters,
                original_id,
                mid,
                1,
                mr.clone_type,
                mr.rule_name,
                mr.subtype,
                "none",
                ov,
                sr,
                rr,
                "positive original-vs-parser-based-mutation",
            )
            positive_count += 1

            mutation_log.append({
                "seed_id": original_id,
                "variant_id": mid,
                "rule_name": mr.rule_name,
                "clone_type": mr.clone_type,
                "mutation_subtype": mr.subtype,
                "accepted": True,
                "position": mr.position,
                "detail": mr.detail,
                "token_overlap": f"{ov:.4f}",
                "line_sr": f"{sr:.4f}",
                "longest_common_token_run_ratio": f"{rr:.4f}",
                "length_ratio": f"{lr:.4f}",
                "parse_ok": True,
                "rustfmt_ok": rustfmt_ok,
                "rustfmt_msg": rustfmt_msg,
            })

        if args.positive_pair_mode == "family_all":
            for i in range(1, len(family_variant_ids)):
                for j in range(i + 1, len(family_variant_ids)):
                    left = family_variant_ids[i]
                    right = family_variant_ids[j]
                    code_l = function_by_id[left]["code"]
                    code_r = function_by_id[right]["code"]
                    ov, sr, rr = compute_pair_metrics(code_l, code_r)
                    add_pair(
                        pairs_out,
                        pair_counters,
                        left,
                        right,
                        1,
                        "Family-all",
                        "same_seed_variant_pair",
                        "same_seed_variant_pair",
                        "none",
                        ov,
                        sr,
                        rr,
                        "additional positive same-seed variant-vs-variant pair",
                    )
                    positive_count += 1

    log(f"Positive pairs: {positive_count}")

    # --------------------
    # Negative sampling
    # --------------------
    # Negative pairs are selected only from original functions and methods.
    # Generated Type-1/Type-2/Type-3 variants are not used as negative candidates.
    originals = [r for r in functions_out if r["variant"] == "original"]

    # Cache normalized tokens and normalized line units because the same
    # functions are compared repeatedly during candidate search.
    metric_cache: Dict[str, Dict[str, Any]] = {}

    def cached_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
        fid = row["bench_func_id"]
        cached = metric_cache.get(fid)
        if cached is not None:
            return cached
        code = row["code"]
        cached = {
            "norm_tokens": norm_tokens(code),
            "norm_lines": norm_line_units(code),
        }
        metric_cache[fid] = cached
        return cached

    used_neg = set()
    neg_low = 0
    neg_similar = 0

    def valid_pair_base(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        # Positive variants from the same seed family must never become negatives.
        if a["seed_family_id"] == b["seed_family_id"]:
            return False
        return True

    def iter_candidate_pairs(
        pool: List[Dict[str, Any]],
        max_checks: int,
    ) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Yield candidate pairs in a deterministic randomized order."""
        n = len(pool)
        if n < 2 or max_checks <= 0:
            return
        order = list(range(n))
        rng.shuffle(order)
        checks = 0
        for pos_i in range(n):
            i = order[pos_i]
            for pos_j in range(pos_i + 1, n):
                j = order[pos_j]
                yield pool[i], pool[j]
                checks += 1
                if checks >= max_checks:
                    return

    # Condition 1:
    # token overlap < 0.25 and N-line-block similarity (SR) < 0.10.
    log(f"Generating low-similarity negatives target={args.target_low_negatives}")
    max_checks_low = (
        args.low_negative_max_checks
        if args.low_negative_max_checks > 0
        else max(args.target_low_negatives * 5000, 200000)
    )
    for a, b in iter_candidate_pairs(originals, max_checks_low):
        if neg_low >= args.target_low_negatives:
            break
        if not valid_pair_base(a, b):
            continue

        left_id, right_id = sorted([a["bench_func_id"], b["bench_func_id"]])
        key = (left_id, right_id)
        if key in used_neg:
            continue

        ma, mb = cached_metrics(a), cached_metrics(b)
        ov = multiset_overlap_max(ma["norm_tokens"], mb["norm_tokens"])
        sr = line_sr_max(ma["norm_lines"], mb["norm_lines"], n=3)
        if ov >= args.low_negative_overlap_max:
            continue
        if sr >= args.low_negative_line_sr_max:
            continue
        rr = run_ratio_min(ma["norm_tokens"], mb["norm_tokens"])

        used_neg.add(key)
        add_pair(
            pairs_out,
            pair_counters,
            left_id,
            right_id,
            0,
            "Low-similarity-negative",
            "low_similarity_pair",
            "none",
            "low_similarity_control",
            ov,
            sr,
            rr,
            "label=0; low token overlap and low N-line-block similarity",
            metric_basis="normalized",
        )
        neg_low += 1

    log(f"Low-similarity negatives: {neg_low}")

    # Condition 2:
    # 0.25 <= token overlap < 0.55,
    # 0.10 <= SR < 0.30, and CR < 0.25.
    log(f"Generating similar-but-discontinuous negatives target={args.target_similar_negatives}")
    max_checks_near = (
        args.similar_negative_max_checks
        if args.similar_negative_max_checks > 0
        else max(args.target_similar_negatives * 10000, 300000)
    )
    for a, b in iter_candidate_pairs(originals, max_checks_near):
        if neg_similar >= args.target_similar_negatives:
            break
        if not valid_pair_base(a, b):
            continue

        left_id, right_id = sorted([a["bench_func_id"], b["bench_func_id"]])
        key = (left_id, right_id)
        if key in used_neg:
            continue

        ma, mb = cached_metrics(a), cached_metrics(b)
        toks_a, toks_b = ma["norm_tokens"], mb["norm_tokens"]
        ov = multiset_overlap_max(toks_a, toks_b)
        if not (args.similar_negative_overlap_min <= ov < args.similar_negative_overlap_max):
            continue

        sr = line_sr_max(ma["norm_lines"], mb["norm_lines"], n=3)
        if not (args.similar_negative_line_sr_min <= sr < args.similar_negative_line_sr_max):
            continue

        rr = run_ratio_min(toks_a, toks_b)
        if rr >= args.similar_negative_max_run_ratio:
            continue

        used_neg.add(key)
        add_pair(
            pairs_out,
            pair_counters,
            left_id,
            right_id,
            0,
            "Similar-but-discontinuous-negative",
            "similar_but_discontinuous_pair",
            "none",
            "similarity_constrained_control",
            ov,
            sr,
            rr,
            "label=0; moderate token/line similarity but short contiguous token match",
            metric_basis="normalized",
        )
        neg_similar += 1

    log(f"Similar-but-discontinuous negatives: {neg_similar}")

    # 各变换规则最终成功生成了多少个，重新实验时可以直接看 summary.json。
    accepted_mutation_counts: Dict[str, int] = {}
    accepted_subtype_counts: Dict[str, int] = {}
    for row in mutation_log:
        if not row.get("accepted"):
            continue
        rule = str(row.get("rule_name", ""))
        subtype = str(row.get("mutation_subtype", ""))
        accepted_mutation_counts[rule] = accepted_mutation_counts.get(rule, 0) + 1
        accepted_subtype_counts[subtype] = accepted_subtype_counts.get(subtype, 0) + 1

    # --------------------
    # Write outputs
    # --------------------
    write_jsonl(out_dir / "selected_seeds.jsonl", selected_seeds_rows)
    write_jsonl(out_dir / "benchmark_functions.jsonl", functions_out)
    write_jsonl(out_dir / "mutation_log.jsonl", mutation_log)
    write_csv(out_dir / "benchmark_pairs.csv", pairs_out)
    write_csv(out_dir / "seed_rejections.csv", seed_rejections)

    summary = {
        "version": "v23",
        "projects": len(projects),
        "seed_candidates_after_filter": len(candidates),
        "seeds_used": len(seeds),
        "functions_generated": len(functions_out),
        "positive_pairs": sum(1 for r in pairs_out if int(r["label"]) == 1),
        "negative_pairs": sum(1 for r in pairs_out if int(r["label"]) == 0),
        "low_similarity_negatives": neg_low,
        "similarity_constrained_negatives": neg_similar,
        "accepted_mutation_counts": accepted_mutation_counts,
        "accepted_subtype_counts": accepted_subtype_counts,
        "filter_reject_counts": filter_counts,
        "rules": {
            "type3_semantics": "Type-3 does not require semantic equivalence or equal execution result; it requires limited parser-guided statement-level additions/deletions/modifications under retained syntactic similarity.",
            "type3_systematic_subtypes": ["insert_inline", "insert_stmt", "insert_fragment", "insert_segmented", "delete_inline", "delete_stmt", "delete_fragment", "delete_segmented", "modify_inline", "modify_stmt", "modify_fragment", "modify_segmented"],
            "insert_fragment_size": "length-aware multi-line insertion: shorter fragments for shorter functions and larger bounded fragments for longer functions; internal statement boundary preferred",
            "insert_segmented_policy": "insert two small statements at two separated internal positions",
            "delete_fragment_size": "exactly two consecutive operation/action/update statements when available; otherwise skipped",
            "delete_segmented_policy": "only for >=150-token functions; delete two non-adjacent operation/action/update statements",
            "modify_policy": "modify_inline first tries let-RHS-to-if and falls back to comparison-operator change; modify_stmt completely replaces one assignment/update statement with a newly defined local declaration; modify_fragment and modify_segmented replace two consecutive or separated eligible statements and skip when no valid positions exist",
            "type1_policy": "Type-1 includes comment add/delete/modify and blank-line add/delete. Ordinary code lines are never deleted as Type-1.",
            "parser_validation": "Every accepted generated positive variant must pass tree-sitter Rust parse validation both as snippet and wrapped Rust file.",
            "negative_policy": "Negative pairs are selected only from different original functions or methods. Condition 1 requires token overlap < 0.25 and SR < 0.10. Condition 2 requires 0.25 <= token overlap < 0.55, 0.10 <= SR < 0.30, and CR < 0.25.",
            "positive_pair_default": args.positive_pair_mode,
        },
        "parameters": vars(args),
    }
    write_text(out_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))

    log(f"Done. Output: {out_dir}")


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build a parser-based Rust clone evaluation dataset.")

    input_group = ap.add_argument_group("input")
    input_group.add_argument("--source-root", default="")
    input_group.add_argument("--result-root", default="")
    input_group.add_argument("--functions-name", default="functions.jsonl")
    input_group.add_argument("--projects-csv", default="")
    input_group.add_argument("--project-root", default="")
    input_group.add_argument("--functions-jsonl", default="")
    input_group.add_argument("--project-name", default="")

    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--num-seeds", type=int, default=0, help="0 = use all selected seeds")
    ap.add_argument("--random-seed", type=int, default=42)
    ap.add_argument("--progress-every", type=int, default=200)

    seed_group = ap.add_argument_group("seed filters")
    seed_group.add_argument("--min-tokens", type=int, default=50)
    seed_group.add_argument("--max-tokens", type=int, default=300)
    seed_group.add_argument("--min-lines", type=int, default=6)
    seed_group.add_argument("--min-statements", type=int, default=3)
    seed_group.add_argument("--macro-threshold", type=int, default=3)
    seed_group.add_argument("--exclude-unsafe", action="store_true", default=True)
    seed_group.add_argument("--include-unsafe", dest="exclude_unsafe", action="store_false")
    seed_group.add_argument("--exclude-const-fn", action="store_true", default=True)
    seed_group.add_argument("--include-const-fn", dest="exclude_const_fn", action="store_false")
    seed_group.add_argument("--free-functions-only", action="store_true", default=False)

    pos_group = ap.add_argument_group("positive generation")
    pos_group.add_argument("--positive-pair-mode", choices=["original_only", "family_all"], default="original_only")
    pos_group.add_argument("--positive-length-ratio-max", type=float, default=1.80)
    pos_group.add_argument("--rustfmt-check", action="store_true")
    pos_group.add_argument("--require-rustfmt", action="store_true")

    neg_group = ap.add_argument_group("negative pair selection")
    neg_group.add_argument("--target-low-negatives", type=int, default=0)
    neg_group.add_argument("--low-negative-overlap-max", type=float, default=0.25)
    neg_group.add_argument("--low-negative-line-sr-max", type=float, default=0.10)
    neg_group.add_argument("--low-negative-max-checks", type=int, default=0, help="0 = auto")

    neg_group.add_argument("--target-similar-negatives", type=int, default=0)
    neg_group.add_argument("--similar-negative-overlap-min", type=float, default=0.25)
    neg_group.add_argument("--similar-negative-overlap-max", type=float, default=0.55)
    neg_group.add_argument("--similar-negative-line-sr-min", type=float, default=0.10)
    neg_group.add_argument("--similar-negative-line-sr-max", type=float, default=0.30)
    neg_group.add_argument("--similar-negative-max-run-ratio", type=float, default=0.25)
    neg_group.add_argument("--similar-negative-max-checks", type=int, default=0, help="0 = auto")

    return ap.parse_args()


def main() -> None:
    args = parse_args()
    try:
        build_benchmark(args)
    except ParserLoadError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
