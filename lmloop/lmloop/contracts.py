"""Deterministic assertion pyramid for lmloop outputs and traces.

Four layers, no model calls and no extra packages:

1. Syntax and types — JSON Schema subset, Python ``ast``, graph DSL, tool args.
2. Contracts — token invariants, exact/regex/ROUGE-1/BLEU-1/bag-of-words cosine,
   rubric faithfulness.
3. Trajectories — tool order, redundant calls, state rollback, abstention.
4. Perturbations — distractors and schema mutations over a frozen case.

Golden cases live in ``evals/golden/``. ``lmloop eval --gate`` is the CI check.
Scores use basis points so a 2% regression compares exactly.
"""

from __future__ import annotations

import ast
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "evals" / "golden"
GOLDEN_CASES = GOLDEN_DIR / "cases.json"
GOLDEN_BASELINE = GOLDEN_DIR / "baseline.json"

# Regressions *greater than* this rate block a pull request (2%).
PR_MAX_DROP = 0.02

_TOKEN = re.compile(r"[a-z0-9]+")
_SECRET_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*\S+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{8,}")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")
# Key names only. ``token`` must not match ``tokenize`` or ``tool_names``.
_SECRET_KEY = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])(?:api[_-]?key|secret|token|password|authorization)(?:$|[^A-Za-z0-9])"
)

_SPAN_FIELDS = ("ts", "trace_id", "span_id", "name")


def _basis(rate: float) -> int:
    """Hundredths of a percent. Avoids the 0.02 float edge."""
    return int(round(float(rate) * 10000))


def drop_exceeds(pass_rate: float, baseline: float, max_drop: float) -> bool:
    """True when ``baseline - pass_rate`` is strictly greater than ``max_drop``."""
    return (_basis(baseline) - _basis(pass_rate)) > _basis(max_drop)


def redact(text: str) -> str:
    """Replace secret-shaped substrings. The rest of the text is kept."""
    out = _SECRET_ASSIGN.sub("[redacted]", text or "")
    out = _BEARER.sub("[redacted]", out)
    return _SK_KEY.sub("[redacted]", out)


def has_secret(text: str) -> bool:
    blob = text or ""
    return bool(_SECRET_ASSIGN.search(blob) or _BEARER.search(blob) or _SK_KEY.search(blob))


def anonymize(value: Any) -> Any:
    """Deep-copy JSON-like data with secrets removed. Used before the inbox."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                out[key] = "[redacted]"
            else:
                out[key] = anonymize(item)
        return out
    if isinstance(value, list):
        return [anonymize(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def rouge1_f1(candidate: str, reference: str) -> float:
    cand = tokens(candidate)
    ref = tokens(reference)
    if not cand or not ref:
        return 0.0
    overlap = sum((Counter(cand) & Counter(ref)).values())
    precision = overlap / len(cand)
    recall = overlap / len(ref)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def bleu1(candidate: str, reference: str) -> float:
    cand = tokens(candidate)
    ref = tokens(reference)
    if not cand or not ref:
        return 0.0
    overlap = sum((Counter(cand) & Counter(ref)).values())
    precision = overlap / len(cand)
    if len(cand) >= len(ref):
        penalty = 1.0
    else:
        penalty = math.exp(1 - len(ref) / len(cand))
    return penalty * precision


def bow_cosine(candidate: str, reference: str) -> float:
    """Bag-of-words cosine. Local stand-in for a rubric embedding, no model."""
    left, right = Counter(tokens(candidate)), Counter(tokens(reference))
    if not left or not right:
        return 0.0
    keys = set(left) | set(right)
    dot = sum(left[k] * right[k] for k in keys)
    norm_l = math.sqrt(sum(v * v for v in left.values()))
    norm_r = math.sqrt(sum(v * v for v in right.values()))
    if norm_l == 0 or norm_r == 0:
        return 0.0
    return dot / (norm_l * norm_r)


def faithfulness(claim: str, context: str) -> float:
    """Share of claim content-words (length >= 4) that appear in ``context``."""
    words = [word for word in tokens(claim) if len(word) >= 4]
    if not words:
        return 1.0
    ctx = set(tokens(context))
    return sum(1 for word in words if word in ctx) / len(words)


def _type_ok(value: Any, expected: str) -> bool:
    # bool is a subclass of int; JSON Schema integer/number exclude it.
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


def _check_schema(value: Any, schema: dict, path: str) -> list[str]:
    expected = schema.get("type")
    if expected and not _type_ok(value, str(expected)):
        return [f"{path}: expected {expected}"]
    failures: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        failures.append(f"{path}: value not in enum")
    if expected == "string" and isinstance(value, str) and "minLength" in schema:
        if len(value) < int(schema["minLength"]):
            failures.append(f"{path}: shorter than minLength")
    if expected in ("integer", "number") and _type_ok(value, str(expected)):
        if "minimum" in schema and value < schema["minimum"]:
            failures.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            failures.append(f"{path}: above maximum")
    if expected == "object" and isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value or value[key] is None:
                failures.append(f"{path}.{key}: missing")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                failures.extend(_check_schema(item, props[key], f"{path}.{key}"))
            elif additional is False:
                failures.append(f"{path}.{key}: additional property")
    if expected == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            failures.extend(_check_schema(item, schema["items"], f"{path}[{index}]"))
    return failures


def _grammar_python(text: str) -> str | None:
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return f"syntax: {exc.msg}"
    return None


def _grammar_graph(text: str) -> str | None:
    from .graph import GraphError, parse_graph

    try:
        parse_graph(text, "golden")
    except GraphError as exc:
        return str(exc)
    return None


_GRAMMARS: dict[str, Callable[[str], str | None]] = {
    "python": _grammar_python,
    "graph": _grammar_graph,
}


def check_schema(case: dict) -> list[str]:
    schema = case.get("schema")
    if not isinstance(schema, dict):
        return ["schema must be an object"]
    if "value" in case:
        value = case["value"]
    elif "text" in case:
        try:
            value = json.loads(case["text"])
        except json.JSONDecodeError as exc:
            return [f"json: {exc.msg}"]
    else:
        return ["schema case needs value or text"]
    return _check_schema(value, schema, "$")


def check_python(case: dict) -> list[str]:
    err = _grammar_python(str(case.get("text") or ""))
    return [err] if err else []


def check_grammar(case: dict) -> list[str]:
    name = str(case.get("parser") or "")
    parser = _GRAMMARS.get(name)
    if parser is None:
        return [f"unknown parser {name!r}"]
    err = parser(str(case.get("text") or ""))
    return [err] if err else []


def check_tool_args(case: dict) -> list[str]:
    from .tools import tool_names, validate_tool_arguments

    name = str(case.get("tool") or "")
    if name not in tool_names():
        return [f"unknown tool {name}"]
    _kwargs, err = validate_tool_arguments(name, case.get("arguments", {}))
    return [err] if err else []


def check_span(span: object) -> list[str]:
    """Structural contract for one local trace span."""
    if not isinstance(span, dict):
        return ["span is not an object"]
    failures: list[str] = []
    for key in _SPAN_FIELDS:
        if not str(span.get(key) or "").strip():
            failures.append(f"missing {key}")
    parent = span.get("parent_span_id", "")
    if not isinstance(parent, str):
        failures.append("parent_span_id must be a string")
    attrs = span.get("attributes", {})
    if attrs is None:
        attrs = {}
    if not isinstance(attrs, dict):
        failures.append("attributes must be an object")
    else:
        try:
            blob = json.dumps(attrs)
        except (TypeError, ValueError) as exc:
            failures.append(f"attributes not json: {exc}")
        else:
            if has_secret(blob):
                failures.append("secret in attributes")
    return failures


def check_span_case(case: dict) -> list[str]:
    return check_span(case.get("span"))


def check_invariant(case: dict) -> list[str]:
    text = str(case.get("text") or "")
    failures: list[str] = []
    for token in case.get("forbid") or []:
        if str(token) in text:
            failures.append(f"forbidden token {token!r}")
    for token in case.get("require") or []:
        if str(token) not in text:
            failures.append(f"missing required token {token!r}")
    if case.get("forbid_secrets") and has_secret(text):
        failures.append("secret leak")
    return failures


def check_metric(case: dict) -> list[str]:
    kind = str(case.get("metric") or "")
    candidate = str(case.get("candidate") or "")
    if kind == "regex":
        pattern = str(case.get("pattern") or "")
        if not pattern:
            return ["metric regex needs a pattern"]
        try:
            matched = re.search(pattern, candidate) is not None
        except re.error as exc:
            return [f"bad pattern: {exc}"]
        return [] if matched else ["regex did not match"]
    scorers = {
        "exact": lambda cand, ref: 1.0 if cand == ref else 0.0,
        "rouge1": rouge1_f1,
        "bleu1": bleu1,
        "cosine": bow_cosine,
    }
    score_fn = scorers.get(kind)
    if score_fn is None:
        return [f"unknown metric {kind!r}"]
    score = float(score_fn(candidate, str(case.get("reference") or "")))
    minimum = float(case.get("min_score", 1.0))
    if score + 1e-9 < minimum:
        return [f"{kind} {score:.3f} < {minimum:.3f}"]
    return []


def check_rubric(case: dict) -> list[str]:
    claim = str(case.get("claim") or "")
    context = str(case.get("context") or "")
    score = faithfulness(claim, context)
    minimum = float(case.get("min_faithfulness", 1.0))
    failures: list[str] = []
    if score + 1e-9 < minimum:
        failures.append(f"faithfulness {score:.3f} < {minimum:.3f}")
    citation = case.get("citation")
    output = str(case.get("output") if case.get("output") is not None else claim)
    if citation and str(citation) not in output:
        failures.append("missing citation")
    return failures


def _is_subsequence(need: list, actual: list) -> bool:
    index = 0
    for name in actual:
        if index < len(need) and name == need[index]:
            index += 1
    return index == len(need)


def _repeated_block(names: list[str]) -> bool:
    """True when the name list ends with the same block twice (a stall cycle)."""
    count = len(names)
    for size in range(2, count // 2 + 1):
        if names[-size:] == names[-2 * size:-size]:
            return True
    return False


def check_trajectory(case: dict) -> list[str]:
    calls = case.get("calls") if case.get("calls") is not None else []
    if not isinstance(calls, list):
        return ["calls must be a list"]
    failures: list[str] = []
    names: list[str] = []
    seen: list[tuple] = []
    allow_repeat = bool(case.get("allow_repeat"))
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or not str(call.get("name") or "").strip():
            failures.append(f"call {index} missing name")
            continue
        name = str(call["name"])
        names.append(name)
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        try:
            key = (name, json.dumps(args, sort_keys=True))
        except (TypeError, ValueError):
            key = (name, repr(args))
        if not allow_repeat and key in seen:
            failures.append(f"redundant call {name}")
        seen.append(key)
    order = list(case.get("order") or [])
    if order and not _is_subsequence(order, names):
        failures.append("tool order is not a subsequence of the trajectory")
    forbid = {str(name) for name in (case.get("forbid_tools") or [])}
    for name in names:
        if name in forbid:
            failures.append(f"forbidden tool {name}")
    max_calls = case.get("max_calls")
    if isinstance(max_calls, int) and len(calls) > max_calls:
        failures.append(f"too many calls ({len(calls)} > {max_calls})")
    if not case.get("allow_cycle") and _repeated_block(names):
        failures.append("cyclic tool stall")
    if case.get("expect_abstain"):
        status = str(case.get("status") or "")
        if status != "abstain":
            failures.append(f"status {status!r} is not abstain")
        if names:
            failures.append("abstain must not call tools")
    before = case.get("state_before")
    after = case.get("state_after")
    if case.get("rollback") and before != after:
        failures.append("rollback did not restore state")
    allowed = case.get("state_keys")
    if allowed is not None and isinstance(after, dict):
        extra = sorted(set(after) - {str(key) for key in allowed})
        if extra:
            failures.append(f"state keys not allowed: {extra}")
    return failures


def _raw_failures(case: dict) -> list[str]:
    kind = str(case.get("kind") or "")
    if kind == "perturbation":
        return ["perturbation cannot nest"]
    checker = CHECKERS.get(kind)
    if checker is None:
        return [f"unknown kind {kind!r}"]
    return list(checker(case))


def check_perturbation(case: dict) -> list[str]:
    inner = dict(case.get("inner") or {})
    if not inner:
        return ["perturbation missing inner"]
    field_name = str(case.get("into") or "")
    if field_name:
        base = str(inner.get(field_name) or "")
        extra = "\n".join(str(item) for item in (case.get("inject") or []))
        inner[field_name] = (base + "\n" + extra).strip()
    patch = case.get("schema_patch")
    if isinstance(patch, dict):
        schema = dict(inner.get("schema") or {})
        schema.update(patch)
        inner["schema"] = schema
    value_patch = case.get("value_patch")
    if isinstance(value_patch, dict) and isinstance(inner.get("value"), dict):
        merged = dict(inner["value"])
        merged.update(value_patch)
        inner["value"] = merged
    return _raw_failures(inner)


CHECKERS: dict[str, Callable[[dict], list[str]]] = {
    "schema": check_schema,
    "python": check_python,
    "grammar": check_grammar,
    "tool_args": check_tool_args,
    "span": check_span_case,
    "invariant": check_invariant,
    "metric": check_metric,
    "rubric": check_rubric,
    "trajectory": check_trajectory,
    "perturbation": check_perturbation,
}


@dataclass(frozen=True)
class GateSpec:
    """One CI gate. ``alerts`` marks a scheduled signal that does not block merge."""

    max_layer: int
    max_drop: float
    alerts: bool


GATES: dict[str, GateSpec] = {
    "commit": GateSpec(max_layer=1, max_drop=0.0, alerts=False),
    "pr": GateSpec(max_layer=3, max_drop=PR_MAX_DROP, alerts=False),
    "nightly": GateSpec(max_layer=4, max_drop=0.0, alerts=True),
}


@dataclass
class CaseResult:
    id: str
    layer: int
    kind: str
    passed: bool
    failures: list[str] = field(default_factory=list)


@dataclass
class SuiteReport:
    results: list[CaseResult]

    @property
    def cases(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 0.0
        return self.passed_count / self.cases

    @property
    def failed(self) -> list[dict]:
        return [
            {"id": result.id, "layer": result.layer, "failures": list(result.failures)}
            for result in self.results
            if not result.passed
        ]


@dataclass
class GateReport:
    gate: str
    ok: bool
    alert: bool
    blocks_merge: bool
    cases: int
    passed: int
    pass_rate: float
    baseline: float
    drop: float
    failed: list[dict]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "ok": self.ok,
            "alert": self.alert,
            "blocks_merge": self.blocks_merge,
            "cases": self.cases,
            "passed": self.passed,
            "pass_rate": self.pass_rate,
            "baseline": self.baseline,
            "drop": self.drop,
            "failed": self.failed,
            "error": self.error,
        }


def _layer_of(case: dict) -> int | None:
    layer = case.get("layer")
    if layer in (1, 2, 3, 4):
        return int(layer)
    return None


def evaluate_case(case: dict) -> CaseResult:
    kind = str(case.get("kind") or "")
    layer = _layer_of(case) or 0
    case_id = str(case.get("id") or "")
    checker = CHECKERS.get(kind)
    if checker is None:
        observed = [f"unknown kind {kind!r}"]
    else:
        try:
            observed = list(checker(case))
        except (TypeError, ValueError, KeyError) as exc:
            observed = [f"{type(exc).__name__}: {exc}"]
    expect = case.get("expect", "pass")
    if expect == "pass":
        passed = not observed
        shown = observed
    elif expect == "fail":
        passed = bool(observed)
        shown = [] if passed else ["expected a contract failure"]
    else:
        passed = False
        shown = [f"expect must be pass or fail, got {expect!r}"]
    if layer == 0:
        passed = False
        shown = ["layer must be 1, 2, 3, or 4"]
    return CaseResult(id=case_id, layer=layer, kind=kind, passed=passed, failures=shown)


def run_cases(cases: list[dict], *, max_layer: int) -> SuiteReport:
    results: list[CaseResult] = []
    invalid = [case for case in cases if _layer_of(case) is None]
    if invalid:
        results.append(CaseResult(
            id="invalid-layer",
            layer=0,
            kind="",
            passed=False,
            failures=[f"{len(invalid)} case(s) missing a layer in 1..4"],
        ))
    seen: set[str] = set()
    for case in cases:
        layer = _layer_of(case)
        if layer is None or layer > max_layer:
            continue
        result = evaluate_case(case)
        if not result.id or result.id in seen:
            result = CaseResult(
                id=result.id or "(missing id)",
                layer=result.layer,
                kind=result.kind,
                passed=False,
                failures=["duplicate or missing id"],
            )
        seen.add(result.id)
        results.append(result)
    if not results:
        results.append(CaseResult(
            id="no-cases",
            layer=0,
            kind="",
            passed=False,
            failures=[f"no cases at layer <= {max_layer}"],
        ))
    return SuiteReport(results)


def load_cases(path: Path | None = None) -> list[dict]:
    raw = json.loads((path or GOLDEN_CASES).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("golden cases must be a JSON list")
    return raw


def load_baseline(path: Path | None = None) -> dict[str, float]:
    raw = json.loads((path or GOLDEN_BASELINE).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("baseline must be a JSON object")
    return {str(key): float(value) for key, value in raw.items()}


def _empty_report(gate: str, error: str, *, blocks: bool) -> GateReport:
    return GateReport(
        gate=gate, ok=False, alert=False, blocks_merge=blocks,
        cases=0, passed=0, pass_rate=0.0, baseline=0.0, drop=0.0,
        failed=[], error=error,
    )


def run_gate(
    name: str,
    *,
    cases: list[dict] | None = None,
    baseline: dict[str, float] | None = None,
) -> GateReport:
    spec = GATES.get(name)
    if spec is None:
        return _empty_report(name, f"unknown gate {name!r}", blocks=True)
    try:
        loaded = load_cases() if cases is None else cases
        rates = load_baseline() if baseline is None else baseline
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _empty_report(name, str(exc), blocks=not spec.alerts)
    if name not in rates:
        return _empty_report(name, f"baseline missing {name!r}", blocks=not spec.alerts)
    suite = run_cases(loaded, max_layer=spec.max_layer)
    base = float(rates[name])
    drop = (_basis(base) - _basis(suite.pass_rate)) / 10000
    exceeded = drop_exceeds(suite.pass_rate, base, spec.max_drop)
    blocks = exceeded and not spec.alerts
    alert = exceeded and spec.alerts
    return GateReport(
        gate=name,
        ok=not blocks and not alert,
        alert=alert,
        blocks_merge=blocks,
        cases=suite.cases,
        passed=suite.passed_count,
        pass_rate=suite.pass_rate,
        baseline=base,
        drop=drop,
        failed=suite.failed,
    )


def format_gate(report: GateReport) -> str:
    lines = [
        f"lmloop eval gate ({report.gate})",
        f"  cases: {report.cases}  passed: {report.passed}"
        f"  rate: {report.pass_rate:.4f}  baseline: {report.baseline:.4f}"
        f"  drop: {report.drop:.4f}",
    ]
    if report.error:
        lines.append(f"  error: {report.error}")
    if report.alert:
        lines.append("  result: alert (distribution shift; does not block merge)")
    elif report.ok:
        lines.append("  result: pass")
    else:
        lines.append("  result: fail")
    for item in report.failed:
        lines.append(f"  failed {item['id']}: {'; '.join(item['failures'])}")
    return "\n".join(lines)
