"""Offline evaluation of the configured extractor on a folder (or zip) of meter photos.

    uv run python -m app.extraction.eval <dir-or-zip> [--out report.json] [--expected truth.json]
                                         [--no-hints] [--no-values] [--limit N]

Uses EXTRACTOR / EXTRACTOR_BASE_URL / EXTRACTOR_API_KEY / EXTRACTOR_MODEL / EXTRACTOR_VERIFY from the
environment (or .env), e.g. to compare models (``--verify`` / ``--no-verify`` overrides EXTRACTOR_VERIFY: a
second, independent read per photo; values it does not reproduce are doubtful; doubles the cost)::

    EXTRACTOR=openai_compat EXTRACTOR_BASE_URL=https://openrouter.ai/api/v1 \\
    EXTRACTOR_API_KEY=... EXTRACTOR_MODEL=google/gemini-2.5-flash \\
    uv run python -m app.extraction.eval ~/fotky/obchuzka.zip --out gemini.json

Hints (known meters, last readings) come from the app DB (DATA_DIR / DATABASE_URL) at each
photo's EXIF time, exactly as in the app; ``--no-hints`` evaluates without the DB and
``--no-values`` keeps meters but hides last/expected values from the prompt.

``--expected`` takes either
- per-photo truth {"<file name>": {"serial": "12345678", "T1": "123.45", "T2": null}} (or a list per file), or
- meter truth of one day built from the DB (``python -m app.extraction.truth 2026-03-01 --out ...``):
  {"kind": "meter_truth", "meters": {"<serial>": {"T1": "12345", ...}}}. Then every value read is compared
  with its meter's truth: ``exact``, ``truncated`` (equal after cutting decimals to whole kWh – the sheet
  stores whole kWh), ``scaled`` (equal only after moving the decimal point: the meter's
  "decimals_shown" in the DB is probably wrong), ``wrong``; and per meter: correct / wrong / mixed /
  unreadable / not found. A *confident* wrong value (confidence >= 0.6, no doubt flag) is the worst outcome
  and is counted separately.
Photos are read in memory; nothing is written except the optional report.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import statistics
import sys
import time
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.extraction.extractors import compare_reads, make_extractor
from app.extraction.images import MAX_UPLOAD_BYTES, ImageRejected, downscale_jpeg, inspect_image
from app.extraction.matching import apply_matching, normalize_serial
from app.extraction.prompt import ParseError, extract_json_object, parse_answer
from app.extraction.types import ExtractionHints, ExtractionResult

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def iter_images(src: Path) -> Iterator[tuple[str, bytes]]:
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file() and p.suffix.lower() in IMAGE_EXT:
                yield p.name, p.read_bytes()
        return
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as zf:
            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                name = Path(info.filename).name
                if info.is_dir() or Path(name).suffix.lower() not in IMAGE_EXT:
                    continue
                if info.file_size > MAX_UPLOAD_BYTES:
                    print(f"{name}: skipped (too large)", file=sys.stderr)
                    continue
                yield name, zf.read(info)
        return
    if src.is_file() and src.suffix.lower() in IMAGE_EXT:
        yield src.name, src.read_bytes()
        return
    raise SystemExit(f"{src}: not a directory, zip or image")


def _hints_factory(no_hints: bool, no_values: bool):
    if no_hints:
        return lambda at: ExtractionHints(include_values=False)
    try:
        from app.config import get_settings
        from app.db import make_engine, make_sessionmaker
        from app.extraction.hints import build_hints
        from app.services.network import load_network

        settings = get_settings()
        engine = make_engine(settings.sqlalchemy_url)
        db = make_sessionmaker(engine)()
        net = load_network(db)
    except Exception as exc:  # no DB yet: evaluate without hints
        print(f"(no hints: {exc})", file=sys.stderr)
        return lambda at: ExtractionHints(include_values=False)

    def factory(at):
        return build_hints(db, at, include_values=not no_values, net=net)

    return factory


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "."))
    except InvalidOperation:
        return None


def score(expected: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Compare one photo's result with the expected meters (serial + register values)."""
    exp_list = expected if isinstance(expected, list) else [expected]
    got = row["meters"]
    out = {"serial_ok": 0, "serial_total": 0, "value_ok": 0, "value_total": 0, "false_values": 0}
    for exp in exp_list:
        serial = normalize_serial(exp.get("serial"))
        match = next((m for m in got if serial and normalize_serial(m.get("serial")) == serial), None)
        if match is None and len(exp_list) == 1 and len(got) == 1:
            match = got[0]
        if serial:
            out["serial_total"] += 1
            out["serial_ok"] += int(match is not None and normalize_serial(match.get("serial")) == serial)
        for reg in ("T1", "T2"):
            want = _dec(exp.get(reg))
            if want is None:
                continue
            out["value_total"] += 1
            vals = [_dec(r["value"]) for r in (match or {}).get("registers", []) if r.get("register") in (reg, None)]
            vals = [v for v in vals if v is not None]
            if want in vals:
                out["value_ok"] += 1
            elif vals:
                out["false_values"] += 1  # a confident wrong number - the worst outcome
    return out


DOUBT = {"low_confidence", "value_mismatch", "same_as_hint", "register_unknown"}
CONFIDENT = 0.6


def _value_outcome(v: Decimal, want: Decimal) -> str:
    if v == want:
        return "exact"
    if v >= 0 and int(v) == want:
        return "truncated"
    for k in (1, 2, 3):
        if int(v.scaleb(-k)) == want or int(v.scaleb(k)) == want:
            return "scaled"
    return "wrong"


def score_meter_truth(truth: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Annotate photos (``row["truth"]``) and summarize per meter against a ``meter_truth`` file."""
    meters: dict[str, dict[str, Any]] = truth["meters"]
    by_norm = {normalize_serial(k): k for k in meters}
    per_meter: dict[str, dict[str, Any]] = {
        k: {"point": v.get("point"), "photos": [], "outcomes": [], "values_read": []} for k, v in meters.items()
    }
    totals = dict.fromkeys(("exact", "truncated", "scaled", "wrong", "no_value", "not_in_truth", "unassigned"), 0)
    totals["confident_wrong"] = 0
    totals["unassigned_fits_truth"] = 0
    totals["plausible_wrong"] = 0
    for row in rows:
        notes = []
        for m in row["meters"]:
            key = m["matched"] if m.get("matched") in meters else by_norm.get(normalize_serial(m.get("serial")))
            how = "serial" if key and normalize_serial(m.get("serial")) else ("match" if key else None)
            if key is None:
                values = [_dec(r["value"]) for r in m["registers"] if _dec(r.get("value")) is not None]
                if m.get("matched") or not values:
                    totals["not_in_truth"] += 1
                    notes.append({"serial": m.get("serial"), "meter": None, "outcome": "not_in_truth"})
                    continue
                # Not assigned to any meter (the review shows it with suggestions): which truth meters would
                # the value be right for (exact / truncated / scaled)?
                for v in values:
                    fits = [
                        f"{k} {g}:{o}"
                        for k, t in meters.items()
                        for g in ("T1", "T2")
                        if (w := _dec(t.get(g))) is not None and (o := _value_outcome(v, w)) != "wrong"
                    ]
                    totals["unassigned"] += 1
                    totals["unassigned_fits_truth"] += int(bool(fits))
                    notes.append(
                        {
                            "serial": m.get("serial"),
                            "meter": None,
                            "outcome": "unassigned",
                            "value": str(v),
                            "fits": fits,
                        }
                    )
                continue
            pm = per_meter[key]
            pm["photos"].append(row["file"])
            values = [r for r in m["registers"] if r.get("value") is not None]
            if not values:
                totals["no_value"] += 1
                pm["outcomes"].append("no_value")
                notes.append({"meter": key, "identified_by": how, "outcome": "no_value"})
                continue
            for r in values:
                v = _dec(r["value"])
                regs = [r["register"]] if r.get("register") in ("T1", "T2") else ["T1", "T2"]
                wants = [(g, _dec(meters[key].get(g))) for g in regs if _dec(meters[key].get(g)) is not None]
                if v is None or not wants:
                    continue
                rank = {"exact": 0, "truncated": 1, "scaled": 2, "wrong": 3}
                outcome, reg = min(((_value_outcome(v, w), g) for g, w in wants), key=lambda x: rank[x[0]])
                confident = r.get("confidence", 0) >= CONFIDENT and not (set(r.get("problems") or []) & DOUBT)
                totals[outcome] += 1
                if outcome == "wrong" and confident:
                    totals["confident_wrong"] += 1
                    rng = r.get("range")
                    # inside the meter's expected range: the review's plausibility check will not warn either
                    lo, hi = (_dec(rng[0]), _dec(rng[1])) if rng else (None, None)
                    if lo is not None and hi is not None and lo <= v <= hi:
                        totals["plausible_wrong"] += 1
                pm["outcomes"].append(outcome)
                pm["values_read"].append(str(v))
                notes.append(
                    {
                        "meter": key,
                        "identified_by": how,
                        "register": reg,
                        "value": str(v),
                        "truth": meters[key].get(reg),
                        "outcome": outcome,
                        "confident": confident,
                    }
                )
        row["truth"] = notes
    status_count: dict[str, int] = {}
    for pm in per_meter.values():
        o = set(pm["outcomes"])
        ok = o & {"exact", "truncated"}
        bad = o & {"wrong", "scaled"}
        pm["status"] = (
            "not_found"
            if not pm["photos"]
            else "unreadable"
            if o <= {"no_value"}
            else "mixed"
            if ok and bad
            else "wrong"
            if bad
            else "correct"
        )
        status_count[pm["status"]] = status_count.get(pm["status"], 0) + 1
    return {"date": truth.get("date"), "values": totals, "meters_by_status": status_count, "meters": per_meter}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.extraction.eval", description=__doc__.split("\n\n")[0])
    ap.add_argument("source", type=Path, help="directory, zip or single image")
    ap.add_argument("--out", type=Path, help="write a JSON report")
    ap.add_argument("--expected", type=Path, help="ground truth JSON {file name: {serial, T1, T2}}")
    ap.add_argument("--no-hints", action="store_true", help="do not use the app DB for hints")
    ap.add_argument("--no-values", action="store_true", help="hints without last/expected values")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-side", type=int, default=None, help="longer image side sent to the model")
    ap.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="second independent model call per photo; a value it does not reproduce = doubt "
        "(default: EXTRACTOR_VERIFY)",
    )
    ap.add_argument("--autocontrast", action="store_true", help="contrast-stretch the image sent to the model")
    ap.add_argument("--concurrency", type=int, default=1, help="parallel model calls (mind rate limits)")
    ap.add_argument(
        "--replay",
        type=Path,
        help="re-use the model answers of an earlier report (no model calls): re-measure matching / meter settings",
    )
    args = ap.parse_args(argv)

    from app.config import get_settings

    settings = get_settings()
    extractor = ReplayExtractor(args.replay) if args.replay else make_extractor(settings)
    if args.verify is not None and hasattr(extractor, "verify"):
        extractor.verify = args.verify
    verify = bool(getattr(extractor, "verify", False))
    max_side = args.max_side or settings.extractor_max_side
    hints_for = _hints_factory(args.no_hints, args.no_values)
    expected = json.loads(args.expected.read_text(encoding="utf-8")) if args.expected else {}
    meter_truth = isinstance(expected, dict) and expected.get("kind") == "meter_truth"
    print(
        f"extractor={settings.extractor} model={settings.extractor_model or '-'} max_side={max_side}"
        f"{' verify' if verify else ''}"
    )

    # Hints come from one DB session (not thread-safe): build them first, then call the model
    # concurrently (bounded), then match and report in file order.
    jobs: list[tuple[str, Any, ExtractionHints, bytes]] = []
    for i, (name, data) in enumerate(iter_images(args.source)):
        if args.limit and i >= args.limit:
            break
        try:
            info = inspect_image(data, settings.tz)
        except ImageRejected as exc:
            print(f"{name}: rejected ({exc.message})")
            continue
        hints = dataclasses.replace(hints_for(info.exif_taken_at), image_sha256=hashlib.sha256(data).hexdigest())
        prepared = b"" if args.replay else downscale_jpeg(data, max_side, autocontrast=args.autocontrast)
        jobs.append((name, info, dataclasses.replace(hints, image_sha256=name) if args.replay else hints, prepared))

    def run(job: tuple[str, Any, ExtractionHints, bytes]) -> tuple[Any, int]:
        t0 = time.monotonic()
        res = extractor.extract(job[3], job[2])
        return res, int((time.monotonic() - t0) * 1000)

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = pool.map(run, jobs)
        for (name, info, hints, _), (res, wall) in zip(jobs, results, strict=True):
            rows.append(_report_row(name, info, hints, res, wall, expected, meter_truth))

    lat = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    summary: dict[str, Any] = {
        "photos": len(rows),
        "errors": sum(1 for r in rows if r["error"]),
        "readable": sum(1 for r in rows if r["readable"]),
        "latency_ms_median": int(statistics.median(lat)) if lat else None,
        "latency_ms_max": max(lat) if lat else None,
        **usage_summary(rows),
    }
    args.verify = verify
    _finish(args, settings, max_side, expected, meter_truth, rows, summary)
    return 0


class ReplayExtractor:
    """Answers from an earlier eval report, looked up by file name (passed as ``hints.image_sha256``)."""

    name = "replay"

    def __init__(self, report: Path) -> None:
        data = json.loads(report.read_text(encoding="utf-8"))
        self.rows = {r["file"]: r for r in data.get("photos", [])}

    def extract(self, image_bytes: bytes, hints: ExtractionHints) -> ExtractionResult:
        row = self.rows.get(hints.image_sha256 or "")
        if row is None:
            return ExtractionResult(problems=["model_error"], error="not in the replayed report")
        raw = row.get("raw")
        if row.get("error") or not isinstance(raw, dict) or not isinstance(raw.get("content"), str):
            return ExtractionResult(
                problems=list(row.get("problems") or []), error=row.get("error") or "no answer", raw=raw
            )
        try:
            res = parse_answer(extract_json_object(raw["content"]))
        except ParseError as exc:
            return ExtractionResult(problems=[exc.code], error=str(exc), raw=raw)
        verify = raw.get("verify")
        if isinstance(verify, dict):  # the report was made with a second read: re-apply its verdicts
            compare_reads(res, _replayed_second(verify))
        res.raw, res.model, res.latency_ms = raw, row.get("model") or "", row.get("latency_ms")
        return res


def _replayed_second(verify: dict[str, Any]) -> ExtractionResult:
    if verify.get("error") or not isinstance(verify.get("content"), str):
        return ExtractionResult(problems=["model_error"], error=verify.get("error") or "no answer")
    try:
        return parse_answer(extract_json_object(verify["content"]))
    except ParseError as exc:
        return ExtractionResult(problems=[exc.code], error=str(exc))


def usage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Tokens and cost reported by the endpoint (OpenRouter returns ``usage.cost`` in USD)."""
    tok_in = tok_out = 0
    cost = 0.0
    seen = False
    for r in rows:
        raw = r.get("raw")
        usage = raw.get("usage") if isinstance(raw, dict) else None
        if not isinstance(usage, dict):
            continue
        seen = True
        # raw["usage"] covers both calls of a verified read ("calls": 2); older reports kept them apart
        legacy_verify = (raw.get("verify") or {}).get("usage") if "calls" not in raw else None
        for u in (usage, legacy_verify):
            if isinstance(u, dict):
                tok_in += int(u.get("prompt_tokens") or 0)
                tok_out += int(u.get("completion_tokens") or 0)
                cost += float(u.get("cost") or 0)
    if not seen:
        return {}
    return {"prompt_tokens": tok_in, "completion_tokens": tok_out, "cost_usd": round(cost, 6)}


def _range(hint: Any, register: str | None) -> list[str] | None:
    """Expected range [low, high] of the matched meter's register (None when unknown)."""
    rh = hint.reg(register) if hint is not None and register else None
    if rh is None or rh.low is None or rh.high is None:
        return None
    return [str(rh.low), str(rh.high)]


def _report_row(
    name: str, info: Any, hints: ExtractionHints, res: Any, wall: int, expected: Any, meter_truth: bool
) -> dict[str, Any]:
    if res.ok:  # also without hints: values are computed from the digits as seen
        apply_matching(res, hints.meters, info.exif_taken_at)
    by_id = {h.meter_id: h for h in hints.meters}
    row = {
        "file": name,
        "taken_at": info.exif_taken_at.isoformat() if info.exif_taken_at else None,
        "latency_ms": res.latency_ms if res.latency_ms is not None else wall,
        "readable": res.readable,
        "problems": res.problems,
        "error": res.error,
        "model": res.model,
        "meters": [
            {
                "serial": m.serial,
                "serial_confidence": m.serial_confidence,
                "matched": by_id[m.meter_id].serial if m.meter_id in by_id else None,
                "match_method": m.match_method,
                "registers": [
                    {
                        "register": r.register,
                        "value": None if r.value is None else str(r.value),
                        "digits_seen": r.digits_seen,
                        "label": r.label_seen,
                        "confidence": r.confidence,
                        "problems": r.problems,
                        "range": _range(by_id.get(m.meter_id), r.register),
                    }
                    for r in m.registers
                ],
            }
            for m in res.meters
        ],
        "raw": res.raw,
    }
    if not meter_truth and name in expected:
        row["score"] = score(expected[name], row)
    regs = "; ".join(
        f"{m['serial'] or '?'}→{m['matched'] or '-'}: "
        + ", ".join(
            f"{r['register'] or '?'}={r['value'] or '?'}" + (f"[{','.join(r['problems'])}]" if r["problems"] else "")
            for r in m["registers"]
        )
        for m in row["meters"]
    )
    status = "ERROR " + (res.error or "") if res.error else ("ok" if res.readable else "unreadable")
    print(f"{name}  {row['taken_at'] or '-':19}  {row['latency_ms']:>6} ms  {status:10}  {regs}", flush=True)
    return row


def _finish(
    args: argparse.Namespace,
    settings: Any,
    max_side: int,
    expected: Any,
    meter_truth: bool,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    scored = [r["score"] for r in rows if "score" in r]
    if scored:
        tot = {k: sum(s[k] for s in scored) for k in scored[0]}
        summary["accuracy"] = {
            **tot,
            "serial_rate": round(tot["serial_ok"] / tot["serial_total"], 3) if tot["serial_total"] else None,
            "value_rate": round(tot["value_ok"] / tot["value_total"], 3) if tot["value_total"] else None,
        }
    if meter_truth:
        mt = score_meter_truth(expected, rows)
        summary["meter_truth"] = {k: v for k, v in mt.items() if k != "meters"}
        print(f"\n--- srovnání s odečty k {mt['date']} (pravda z databáze) ---")
        for row in rows:
            for n in row.get("truth", []):
                if n["outcome"] == "not_in_truth":
                    print(f"{row['file']}: {n.get('serial') or '?'} – není v pravdě (jiný elektroměr / nepřiřazeno)")
                elif n["outcome"] == "no_value":
                    print(f"{row['file']}: {n['meter']} – bez hodnoty")
                elif n["outcome"] == "unassigned":
                    fits = f" (sedí na {n['fits']})" if n.get("fits") else ""
                    print(f"{row['file']}: {n.get('serial') or '?'} nepřiřazeno, přečteno {n['value']}{fits}")
                else:
                    flag = " (JISTĚ ŠPATNĚ)" if n["outcome"] == "wrong" and n["confident"] else ""
                    print(
                        f"{row['file']}: {n['meter']} {n['register']} přečteno {n['value']}, "
                        f"pravda {n['truth']} → {n['outcome']}{flag} [určeno: {n['identified_by']}]"
                    )
        print("\nelektroměr              stav         fotky  hodnoty")
        for serial, pm in mt["meters"].items():
            print(f"{serial:22}  {pm['status']:11}  {len(pm['photos']):5}  {', '.join(pm['values_read']) or '-'}")
        print(
            f"\nhodnoty: {mt['values']} | elektroměry: {mt['meters_by_status']} | "
            f"jistě špatně: {mt['values']['confident_wrong']} "
            f"(z toho v očekávaném rozsahu: {mt['values']['plausible_wrong']})"
        )
        summary["meter_truth"]["meters"] = mt["meters"]
    print(json.dumps({k: v for k, v in summary.items() if k != "meter_truth"}, ensure_ascii=False))
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "settings": {
                        "extractor": settings.extractor,
                        "model": settings.extractor_model,
                        "max_side": max_side,
                        "autocontrast": args.autocontrast,
                        "verify": bool(args.verify),
                    },
                    "summary": summary,
                    "photos": rows,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"report: {args.out}")


if __name__ == "__main__":
    raise SystemExit(main())
