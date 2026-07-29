"""
Tools for the quote-to-order agent.

get_opportunity_data(opportunity_id):
    Retrieves every parsed file stored under
    <GCS_BASE_PATH>/<opportunity_id>/  (configured in the .env file at the
    project root, e.g. GCS_BASE_PATH=gs://vfie-dh-customer-fixed)
    and stores the combined
    content in session state under "opportunity_data" so that
    downstream agents (the BSP extractor) can reuse it without
    re-downloading.
"""

import os
import json
import re
from datetime import date, datetime
from google.cloud import storage
from google.adk.tools import ToolContext

from .config import (
    GCS_BUCKET_NAME, GCS_BASE_PREFIX, PROJECT_ID,
    MAX_DOC_CHARS, MAX_TOTAL_DOC_CHARS, MAX_TECHSPEC_CHARS,
)

STATUS_FILENAME = "validation_status.json"

# Keys like "unnamed_01", "unnamed: 12", "Unnamed_3" are empty spreadsheet
# columns emitted by the parser. When their value is empty they carry no
# validation value and can bloat a parsed file to hundreds of MB, so we drop
# them at ingestion. Non-empty unnamed_* values are KEPT (they may hold real
# data under a missing header).
_UNNAMED_RE = re.compile(
    r"^\s*(?:unnamed|__?empty|column|col|field|var|x)?[\s_:\-\.]*\d+\s*$",
    re.IGNORECASE,
)


def _is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "nan", "n/a"):
        return True
    if isinstance(v, list):
        return all(_is_empty(x) for x in v)
    if isinstance(v, dict):
        return all(_is_empty(x) for x in v.values())
    return False


def _prune_empty(obj):
    """Recursively remove empty 'unnamed_*' keys and drop empty rows.

    Returns (cleaned_obj, removed_count). Real data is never removed - only
    keys whose name is an unnamed placeholder AND whose value is empty, plus
    fully-empty dict/list entries left behind.
    """
    removed = 0

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _UNNAMED_RE.match(str(k)) and _is_empty(v):
                removed += 1
                continue
            cleaned, r = _prune_empty(v)
            removed += r
            # drop a key that became empty only if it was an unnamed placeholder
            if _UNNAMED_RE.match(str(k)) and _is_empty(cleaned):
                removed += 1
                continue
            out[k] = cleaned
        return out, removed

    if isinstance(obj, list):
        out_list = []
        for item in obj:
            cleaned, r = _prune_empty(item)
            removed += r
            # drop rows that are entirely empty after pruning
            if _is_empty(cleaned):
                removed += 1
                continue
            out_list.append(cleaned)
        return out_list, removed

    return obj, removed


# ─────────────────────────────────────────────────────────────────────
# Deterministic size-budgeting so the payload never exceeds the model cap
# ─────────────────────────────────────────────────────────────────────
# Total character budget for ALL documents combined that we hand to an agent.
# The model rejects oversized inputs ("constraint is too tall"); this keeps
# us safely under it. Tune via env if the proxy cap changes.
CONDENSE_TOTAL_BUDGET = int(os.getenv("CONDENSE_TOTAL_BUDGET", "180000"))
# Per-document ceiling for NON priority docs (tech spec / VBOP get more).
CONDENSE_PER_DOC_BUDGET = int(os.getenv("CONDENSE_PER_DOC_BUDGET", "40000"))
# Clip individual string values longer than this.
CONDENSE_MAX_STR = int(os.getenv("CONDENSE_MAX_STR", "2000"))
# Cap how many rows we keep from a huge array of records.
CONDENSE_MAX_ROWS = int(os.getenv("CONDENSE_MAX_ROWS", "400"))


def _clip_strings(obj, max_str):
    """Clip over-long string values (they're usually raw text blobs)."""
    if isinstance(obj, str):
        if len(obj) > max_str:
            return obj[:max_str] + f"...[clipped {len(obj) - max_str} chars]"
        return obj
    if isinstance(obj, dict):
        return {k: _clip_strings(v, max_str) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clip_strings(v, max_str) for v in obj]
    return obj


def _cap_rows(obj, max_rows):
    """Cap very long arrays of records, keeping the first N and noting the rest."""
    if isinstance(obj, list) and len(obj) > max_rows:
        kept = [_cap_rows(x, max_rows) for x in obj[:max_rows]]
        kept.append({"_note": f"{len(obj) - max_rows} more rows omitted to fit size budget"})
        return kept
    if isinstance(obj, list):
        return [_cap_rows(x, max_rows) for x in obj]
    if isinstance(obj, dict):
        return {k: _cap_rows(v, max_rows) for k, v in obj.items()}
    return obj


def _size(obj) -> int:
    try:
        return len(obj if isinstance(obj, str) else json.dumps(obj, default=str))
    except Exception:  # noqa: BLE001
        return len(str(obj))


def _compact_doc(content, per_doc_budget):
    """Compact one document: clip long strings, cap huge row arrays, and if it
    is still over budget, hard-truncate its serialized form as a last resort."""
    content = _clip_strings(content, CONDENSE_MAX_STR)
    content = _cap_rows(content, CONDENSE_MAX_ROWS)
    if _size(content) <= per_doc_budget:
        return content
    # last resort: serialise and truncate, keeping valid-ish text
    s = content if isinstance(content, str) else json.dumps(content, default=str)
    return {"_truncated_document": s[:per_doc_budget] + f"...[clipped, doc exceeded {per_doc_budget} chars]"}


def _condense_documents(documents, tech_spec_files, vbop_files, total_budget):
    """Return (condensed_documents, report).

    Priority: tech spec and VBOP are kept as fully as possible; other files are
    compacted and, if the combined size still exceeds total_budget, dropped to a
    short placeholder (never silently lost - a note is left).
    """
    priority = set(tech_spec_files or []) | set(vbop_files or [])
    report = {"size_before": _size(documents), "compacted_files": [], "dropped_files": [],
              "truncated": False}

    # Priority docs get a bigger per-doc budget; others the standard one.
    prio_budget = max(CONDENSE_PER_DOC_BUDGET * 3, total_budget // 2)

    condensed = {}
    # 1) place priority docs first, compacted to their larger budget
    for name in list(documents.keys()):
        if name in priority:
            condensed[name] = _compact_doc(documents[name], prio_budget)

    # 2) then the rest, compacted to the standard per-doc budget
    for name, content in documents.items():
        if name in priority:
            continue
        condensed[name] = _compact_doc(content, CONDENSE_PER_DOC_BUDGET)

    # 3) enforce the TOTAL budget: if still over, drop non-priority docs
    #    (largest first) to short placeholders until we fit.
    def total():
        return _size(condensed)

    if total() > total_budget:
        report["truncated"] = True
        non_prio = sorted(
            [n for n in condensed if n not in priority],
            key=lambda n: _size(condensed[n]), reverse=True,
        )
        for name in non_prio:
            if total() <= total_budget:
                break
            condensed[name] = {"_omitted": f"'{name}' omitted to fit the size budget; "
                                          "compact fields were kept where possible."}
            report["dropped_files"].append(name)

    # record which files were compacted vs left whole
    for name in documents:
        if _size(condensed.get(name)) < _size(documents[name]):
            report["compacted_files"].append(name)
    report["size_after"] = total()
    if report["size_after"] < report["size_before"]:
        report["truncated"] = True
    return condensed, report


_storage_client = None


# ─────────────────────────────────────────────────────────────────────
# Deterministic size control (pure Python - never calls the model, so it
# can never hit the proxy's request-size cap). Guarantees each document,
# and the whole set, stays under a character budget before it ever reaches
# an agent.
# ─────────────────────────────────────────────────────────────────────
def _doc_len(obj) -> int:
    try:
        return len(obj if isinstance(obj, str) else json.dumps(obj, default=str))
    except Exception:  # noqa: BLE001
        return len(str(obj))


# Keywords the validation/extraction checks actually compare on. When a
# document is too big, we keep the rows/lines/keys that mention any of these
# and drop the rest, so nothing check-relevant is lost while the bulk goes.
_RELEVANT_KEYS = (
    "customer", "company", "client", "bill", "ship", "account",
    "opportunity", "opp", "reference", "ref", "po", "purchase order",
    "quote", "vendor", "supplier", "price", "cost", "sell", "margin",
    "product", "code", "sku", "part", "item", "quantity", "qty",
    "site", "address", "eircode", "premise", "location", "contact",
    "email", "mobile", "phone", "name",
    "contract", "term", "speed", "bandwidth", "service", "circuit",
    "date", "expiry", "valid", "lead", "leadtime",
    "qos", "af", "ef", "recurring", "billing", "frequency",
    "eir", "enet", "siro", "ripplecom", "nni", "eil", "sab", "qdc",
)


def _mentions_relevant(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in _RELEVANT_KEYS)


def _shrink_to_budget(name, content, budget):
    """Reduce a single document below `budget` characters, deterministically.

    Strategy, in order, keeping check-relevant content:
      - list of row-dicts (spreadsheet): keep rows that mention relevant keys,
        then cap the number of rows, noting how many were omitted.
      - dict: keep relevant keys; truncate very long string values.
      - text: keep relevant lines; head+tail truncate if still over.
    Returns (shrunk_content, note or None).
    """
    if _doc_len(content) <= budget:
        return content, None

    # 1) List of rows (most common bloat shape)
    if isinstance(content, list):
        rows = content
        # Keep rows that still carry real (non-empty) content after pruning,
        # relevant ones first; these are what the checks actually read.
        def _row_score(r):
            s = json.dumps(r, default=str)
            has_data = not _is_empty(r) and s not in ("{}", "[]", "null")
            return (0 if _mentions_relevant(s) else 1, 0 if has_data else 1)
        ordered = sorted(rows, key=_row_score)
        kept, used = [], 0
        for r in ordered:
            if _is_empty(r):
                continue
            rlen = _doc_len(r) + 2
            if used + rlen > budget - 200:  # leave room for the note
                break
            kept.append(r)
            used += rlen
        omitted = len(rows) - len(kept)
        note = (f"[shrunk: kept {len(kept)} of {len(rows)} rows "
                f"(data/relevant-first), {omitted} omitted to fit size budget]")
        result = kept + [{"_note": note}]
        # hard guarantee under budget
        while _doc_len(result) > budget and len(result) > 1:
            result.pop(0)
        return result, note

    # 2) Dict: keep relevant keys, truncate long values
    if isinstance(content, dict):
        out, used, dropped = {}, 0, 0
        # relevant keys first
        items = sorted(content.items(),
                       key=lambda kv: 0 if _mentions_relevant(str(kv[0])) else 1)
        for k, v in items:
            vs = v if isinstance(v, str) else json.dumps(v, default=str)
            if len(vs) > 2000:
                vs = vs[:2000] + "...[truncated]"
                v = vs
            klen = len(str(k)) + len(vs) + 4
            if used + klen > budget:
                dropped += 1
                continue
            out[k] = v
            used += klen
        if dropped:
            out["_note"] = f"[shrunk: {dropped} less-relevant keys dropped to fit size budget]"
        return out, out.get("_note")

    # 3) Plain text: keep relevant lines, else head+tail
    text = content if isinstance(content, str) else str(content)
    lines = text.splitlines()
    relevant_lines = [ln for ln in lines if _mentions_relevant(ln)]
    joined = "\n".join(relevant_lines)
    if relevant_lines and len(joined) <= budget:
        return (joined + f"\n[shrunk: kept {len(relevant_lines)} of {len(lines)} "
                f"relevant lines to fit size budget]"), "shrunk-text"
    marker = "\n...[middle truncated to fit size budget]...\n"
    half = max(0, (budget - len(marker)) // 2)
    return (text[:half] + marker + text[-half:]), "truncated-text"





def _client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client(project=PROJECT_ID)
    return _storage_client


# Filenames that identify the tech spec document. Folder names vary but the
# filename always contains something like "tech spec" / "tec spec" /
# "tech_spec" / "techspec" (any case, any separator).
_TECH_SPEC_RE = re.compile(r"te[ck]h?(?:nical)?[\s_\-\.]*spec", re.IGNORECASE)


def _is_tech_spec(blob_name: str) -> bool:
    filename = blob_name.rsplit("/", 1)[-1]
    return bool(_TECH_SPEC_RE.search(filename))


def _strip_ext(filename: str) -> str:
    # drop compound extensions like ".pdf.json" -> "file"
    name = filename.rsplit("/", 1)[-1]
    for _ in range(2):
        base, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 5:
            name = base
        else:
            break
    return name


def _is_vbop(blob_name: str, opportunity_id: str) -> bool:
    """The VBOP file is named ONLY by the opportunity id, e.g. 'opp-1234567'.
    Match when the filename stem equals the opp id (case-insensitive), optionally
    with an 'opp-'/'opp_' prefix."""
    stem = _strip_ext(blob_name).strip().lower()
    oid = opportunity_id.strip().lower()
    candidates = {oid, f"opp-{oid}", f"opp_{oid}", f"opp{oid}"}
    # also handle stems that are exactly the id with an opp prefix already
    return stem in candidates or stem.lstrip("opp-_") == oid.lstrip("opp-_")


def get_opportunity_data(opportunity_id: str, tool_context: ToolContext) -> dict:
    """Retrieve all parsed documents for a given opportunity id from GCS.

    The bucket is organised as one folder per opportunity:
        gs://<bucket>/<base prefix>/<folder containing the opportunity id>/<parsed files>

    The folder name does not need to equal the opportunity id exactly -
    it only needs to CONTAIN it (e.g. folder "4234-opp-5555363773" matches
    opportunity id "5555363773").

    Args:
        opportunity_id: The opportunity id to look for inside folder names.

    Returns:
        dict with:
            status: "success" | "not_found" | "error"
            opportunity_id: echo of the input
            files: list of {name, size_bytes, loaded}
            documents: {filename: parsed content} for every loaded file
    """
    opportunity_id = opportunity_id.strip().strip("/")
    base_prefix = f"{GCS_BASE_PREFIX}/" if GCS_BASE_PREFIX else ""

    ## Folder names are not always exactly the opp id - they may just
    ## CONTAIN it (e.g. "4234-opp-5555363773" for opp id "5555363773").
    ## So first list the folders under the base path and match by substring.
    try:
        bucket = _client().bucket(GCS_BUCKET_NAME)

        listing = bucket.list_blobs(prefix=base_prefix, delimiter="/")
        # .prefixes is only populated after the iterator is consumed
        _ = list(listing)
        folders = sorted(listing.prefixes)  # e.g. ["base/4234-opp-5555363773/", ...]
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "opportunity_id": opportunity_id,
            "message": f"Failed to list gs://{GCS_BUCKET_NAME}/{base_prefix}: {exc}",
        }

    def _folder_name(full_prefix: str) -> str:
        return full_prefix[len(base_prefix):].strip("/")

    matches = [
        p for p in folders
        if opportunity_id.lower() in _folder_name(p).lower()
    ]

    if not matches:
        return {
            "status": "not_found",
            "opportunity_id": opportunity_id,
            "message": (
                f"No folder under gs://{GCS_BUCKET_NAME}/{base_prefix} contains "
                f"'{opportunity_id}' in its name. Check the opportunity id or "
                "confirm the parsed files were uploaded."
            ),
            "available_folders": [_folder_name(p) for p in folders][:50],
        }

    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "opportunity_id": opportunity_id,
            "message": (
                f"Multiple folders contain '{opportunity_id}'. "
                "Report the candidates and ask which one to use, or retry "
                "with a more specific opportunity id."
            ),
            "matching_folders": [_folder_name(p) for p in matches],
        }

    prefix = matches[0]
    matched_folder = _folder_name(prefix)

    try:
        blobs = [b for b in bucket.list_blobs(prefix=prefix) if not b.name.endswith("/")]
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "opportunity_id": opportunity_id,
            "message": f"Failed to access gs://{GCS_BUCKET_NAME}/{prefix}: {exc}",
        }

    if not blobs:
        return {
            "status": "not_found",
            "opportunity_id": opportunity_id,
            "matched_folder": matched_folder,
            "message": (
                f"Folder gs://{GCS_BUCKET_NAME}/{prefix} matched the opportunity id "
                "but contains no files."
            ),
        }

    documents: dict = {}
    manifest: list = []

    for blob in blobs:
        # Never re-ingest our own saved validation status as a "document".
        if blob.name.rsplit("/", 1)[-1] == STATUS_FILENAME:
            continue

        entry = {
            "name": blob.name,
            "size_bytes": blob.size,
            "loaded": False,
            "is_tech_spec": _is_tech_spec(blob.name),
        }

        try:
            raw = blob.download_as_bytes()
            text = raw.decode("utf-8", errors="replace")

            # Try to parse as JSON regardless of extension - the huge
            # spreadsheet-style files may not always be named .json.
            parsed_json = None
            try:
                parsed_json = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed_json = None

            if parsed_json is not None:
                before = len(text)
                parsed_json, removed = _prune_empty(parsed_json)
                if removed:
                    entry["pruned_empty_fields"] = removed
                    after = len(json.dumps(parsed_json))
                    entry["size_before"] = before
                    entry["size_after"] = after
                    print(f"[prune] {blob.name}: removed {removed} empty auto-keys, "
                          f"{before} -> {after} chars "
                          f"({100 * (before - after) // max(before, 1)}% smaller)")
                content = parsed_json
            else:
                content = text

            # Deterministic size cap per document (bigger share for tech spec).
            budget = MAX_TECHSPEC_CHARS if entry["is_tech_spec"] else MAX_DOC_CHARS
            pre = _doc_len(content)
            if pre > budget:
                content, note = _shrink_to_budget(blob.name, content, budget)
                entry["shrunk"] = True
                entry["size_before_shrink"] = pre
                entry["size_after_shrink"] = _doc_len(content)
                print(f"[shrink] {blob.name}: {pre} -> {entry['size_after_shrink']} "
                      f"chars (budget {budget}) {note or ''}")

            documents[blob.name] = content
            entry["loaded"] = True
        except Exception as exc:  # noqa: BLE001
            entry["skipped_reason"] = f"download/decode failed: {exc}"

        manifest.append(entry)

    # Persist for downstream agents (BSP extractor) via session state.
    tech_spec_files = [e["name"] for e in manifest if e["is_tech_spec"] and e["loaded"]]

    # The VBOP file is named only by the opportunity id.
    for e in manifest:
        e["is_vbop"] = _is_vbop(e["name"], opportunity_id)
    vbop_files = [e["name"] for e in manifest if e["is_vbop"] and e["loaded"]]

    # Existing validation status saved by a previous run (if any).
    existing_status = None
    status_blob_name = f"{prefix}{STATUS_FILENAME}"
    try:
        sb = bucket.blob(status_blob_name)
        if sb.exists():
            existing_status = json.loads(sb.download_as_text())
    except Exception:  # noqa: BLE001 - existing status is best effort
        existing_status = None

    # Total-budget backstop: if the combined documents still exceed the total
    # budget, shrink the largest non-tech-spec docs further until they fit.
    def _total_len(docs):
        return sum(_doc_len(v) for v in docs.values())

    total = _total_len(documents)
    if total > MAX_TOTAL_DOC_CHARS:
        techspec_names = {e["name"] for e in manifest if e["is_tech_spec"]}
        # shrink biggest non-tech-spec docs first
        shrinkable = sorted(
            (n for n in documents if n not in techspec_names),
            key=lambda n: _doc_len(documents[n]), reverse=True,
        )
        for n in shrinkable:
            if _total_len(documents) <= MAX_TOTAL_DOC_CHARS:
                break
            cur = _doc_len(documents[n])
            headroom = max(2000, cur - (total - MAX_TOTAL_DOC_CHARS))
            documents[n], _ = _shrink_to_budget(n, documents[n], min(cur, headroom))
        print(f"[total-budget] combined docs {total} -> {_total_len(documents)} "
              f"chars (budget {MAX_TOTAL_DOC_CHARS})")

    tool_context.state["opportunity_id"] = opportunity_id
    tool_context.state["template_date"] = date.today().isoformat()
    tool_context.state["opportunity_folder"] = matched_folder
    tool_context.state["opportunity_prefix"] = prefix  # full GCS prefix for save/load
    tool_context.state["opportunity_file_manifest"] = manifest
    tool_context.state["tech_spec_files"] = tech_spec_files
    tool_context.state["vbop_files"] = vbop_files
    tool_context.state["existing_validation_status"] = existing_status

    # Guarantee the payload handed to the agents stays under the model's
    # input cap, no matter how large a single file is. Tech spec + VBOP are
    # preserved as fully as possible; other documents are compacted (long
    # strings clipped, huge row-arrays sampled). Deterministic, no model call.
    condensed, condense_report = _condense_documents(
        documents,
        tech_spec_files=tech_spec_files,
        vbop_files=vbop_files,
        total_budget=CONDENSE_TOTAL_BUDGET,
    )
    tool_context.state["opportunity_data"] = condensed
    tool_context.state["opportunity_data_full"] = documents  # raw, if ever needed
    tool_context.state["condense_report"] = condense_report
    if condense_report.get("truncated"):
        print(f"[condense] payload {condense_report['size_before']} -> "
              f"{condense_report['size_after']} chars (budget {CONDENSE_TOTAL_BUDGET}); "
              f"compacted: {condense_report['compacted_files']}")

    return {
        "status": "success",
        "opportunity_id": opportunity_id,
        "matched_folder": matched_folder,
        "tech_spec_files": tech_spec_files or "WARNING: no tech spec document found in this folder",
        "vbop_files": vbop_files or "WARNING: no VBOP file (named by opportunity id) found in this folder",
        "existing_validation_status": existing_status or "none - first run for this opportunity",
        "condense_report": condense_report,
        "files": manifest,
        "documents": condensed,
    }


# ─────────────────────────────────────────────────────────────────────
# BigQuery lookups for BSP internal validation
# ─────────────────────────────────────────────────────────────────────
from .config import BQ_PROJECT, BQ_DATASET

_bq_client = None


def _bq():
    global _bq_client
    if _bq_client is None:
        from google.cloud import bigquery
        _bq_client = bigquery.Client(project=BQ_PROJECT)
    return _bq_client


def _rows_to_dicts(job, limit=25):
    out = []
    for i, row in enumerate(job):
        if i >= limit:
            break
        out.append({k: (str(v) if v is not None else None) for k, v in dict(row).items()})
    return out


def lookup_bsp_company(name: str, tool_context: ToolContext) -> dict:
    """Look up a customer / company in BSP (BigQuery TEMP_company).

    Tries an exact UPPER(TRIM(name)) match first, then a LIKE contains-match,
    so the agent can judge the closest match. Use for:
      - customer name validation (base check 1)
      - billing entity / bill_to_name validation (base check 3)

    Args:
        name: The customer or billing entity name from the tech spec / order form.

    Returns:
        dict with exact_matches, like_matches (closest candidates), and counts.
    """
    from google.cloud import bigquery

    table = f"`{BQ_PROJECT}.{BQ_DATASET}.TEMP_company`"
    safe = (name or "").strip()
    params = [bigquery.ScalarQueryParameter("val", "STRING", safe.upper())]
    cfg = bigquery.QueryJobConfig(query_parameters=params)

    try:
        exact_sql = f"SELECT * FROM {table} WHERE UPPER(TRIM(`name`)) = @val"
        exact = _rows_to_dicts(_bq().query(exact_sql, job_config=cfg))

        like_cfg = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("val", "STRING", f"%{safe.upper()}%")
        ])
        like_sql = f"SELECT * FROM {table} WHERE UPPER(TRIM(`name`)) LIKE @val LIMIT 25"
        like = _rows_to_dicts(_bq().query(like_sql, job_config=like_cfg))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "query": name, "message": str(exc)}

    return {
        "status": "success",
        "query": name,
        "exact_match_count": len(exact),
        "exact_matches": exact,
        "like_match_count": len(like),
        "like_matches": like,
    }


def lookup_bsp_location(location: str, eircode: str, tool_context: ToolContext) -> dict:
    """Look up a delivery / service address in BSP (BigQuery TEMP_location).

    Matches against the concatenated name + address_1..address_3 via a
    contains (LIKE) search. If an Eircode is provided it is tried as an
    additional, more precise search. Use for:
      - delivery address validation (base check 2)
      - service address validation (circuit check 6)

    Args:
        location: The address string from the tech spec / circuit order form.
        eircode: Optional Eircode; pass "" if none.

    Returns:
        dict with address_matches, eircode_matches (if any), and counts.
    """
    from google.cloud import bigquery

    table = f"`{BQ_PROJECT}.{BQ_DATASET}.TEMP_location`"
    concat = ("UPPER(CONCAT("
              "IFNULL(`name`,''),' ',"
              "IFNULL(`address_1`,''),' ',"
              "IFNULL(`address_2`,''),' ',"
              "IFNULL(`address_3`,'')"
              "))")
    result = {"status": "success", "query": location, "eircode": eircode or None}

    try:
        loc_cfg = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("val", "STRING", f"%{(location or '').strip().upper()}%")
        ])
        loc_sql = f"SELECT * FROM {table} WHERE {concat} LIKE @val LIMIT 25"
        addr = _rows_to_dicts(_bq().query(loc_sql, job_config=loc_cfg))
        result["address_match_count"] = len(addr)
        result["address_matches"] = addr

        if eircode and eircode.strip():
            ec_cfg = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("val", "STRING", f"%{eircode.strip().upper()}%")
            ])
            ec_sql = f"SELECT * FROM {table} WHERE {concat} LIKE @val LIMIT 25"
            ec = _rows_to_dicts(_bq().query(ec_sql, job_config=ec_cfg))
            result["eircode_match_count"] = len(ec)
            result["eircode_matches"] = ec
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "query": location, "message": str(exc)}

    return result


# ─────────────────────────────────────────────────────────────────────
# Persist / retrieve validation status in the opportunity's GCS folder
# so re-runs show the latest status and other users can track it.
# ─────────────────────────────────────────────────────────────────────
def save_validation_status(status_json: str, tool_context: ToolContext) -> dict:
    """Save the full validation status back to the opportunity's GCS folder
    as validation_status.json, so it is retrieved on future runs and other
    users can track progress.

    Args:
        status_json: The complete validation status as a JSON string. Should
            contain the checks and their results/notes/actions and any human
            feedback records (option, comment, saved date).

    Returns:
        dict with the GCS path written, or an error.
    """
    prefix = tool_context.state.get("opportunity_prefix")
    opp_id = tool_context.state.get("opportunity_id", "")
    if not prefix:
        return {"status": "error",
                "message": "No opportunity_prefix in state - call get_opportunity_data first."}

    try:
        payload = json.loads(status_json) if isinstance(status_json, str) else status_json
    except json.JSONDecodeError as exc:
        return {"status": "error", "message": f"status_json is not valid JSON: {exc}"}

    # stamp the save time
    payload.setdefault("opportunity_id", opp_id)
    payload["last_saved_utc"] = datetime.utcnow().isoformat() + "Z"

    blob_name = f"{prefix}{STATUS_FILENAME}"
    try:
        bucket = _client().bucket(GCS_BUCKET_NAME)
        bucket.blob(blob_name).upload_from_string(
            json.dumps(payload, indent=2), content_type="application/json"
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"Failed to write gs://{GCS_BUCKET_NAME}/{blob_name}: {exc}"}

    tool_context.state["existing_validation_status"] = payload
    return {"status": "success",
            "path": f"gs://{GCS_BUCKET_NAME}/{blob_name}",
            "last_saved_utc": payload["last_saved_utc"]}


def get_validation_status(opportunity_id: str, tool_context: ToolContext) -> dict:
    """Retrieve the previously saved validation_status.json for an opportunity
    (without re-downloading all documents). Resolves the folder the same way
    get_opportunity_data does.

    Args:
        opportunity_id: The opportunity id.

    Returns:
        dict with the saved status, or status "not_found".
    """
    opportunity_id = opportunity_id.strip().strip("/")
    base_prefix = f"{GCS_BASE_PREFIX}/" if GCS_BASE_PREFIX else ""
    try:
        bucket = _client().bucket(GCS_BUCKET_NAME)
        listing = bucket.list_blobs(prefix=base_prefix, delimiter="/")
        _ = list(listing)
        folders = sorted(listing.prefixes)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}

    def _fn(p):
        return p[len(base_prefix):].strip("/")

    matches = [p for p in folders if opportunity_id.lower() in _fn(p).lower()]
    if not matches:
        return {"status": "not_found", "opportunity_id": opportunity_id,
                "message": "No folder contains this opportunity id."}
    prefix = matches[0]
    blob_name = f"{prefix}{STATUS_FILENAME}"
    try:
        blob = bucket.blob(blob_name)
        if not blob.exists():
            return {"status": "not_found", "opportunity_id": opportunity_id,
                    "message": "No saved validation status yet."}
        return {"status": "success", "opportunity_id": opportunity_id,
                "validation_status": json.loads(blob.download_as_text())}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}
