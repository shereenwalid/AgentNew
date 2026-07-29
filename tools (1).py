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

import json
import re
from datetime import date, datetime
from google.cloud import storage
from google.adk.tools import ToolContext

from .config import GCS_BUCKET_NAME, GCS_BASE_PREFIX, PROJECT_ID

STATUS_FILENAME = "validation_status.json"

# Keys like "unnamed_01", "unnamed: 12", "Unnamed_3" are empty spreadsheet
# columns emitted by the parser. When their value is empty they carry no
# validation value and can bloat a parsed file to hundreds of MB, so we drop
# them at ingestion. Non-empty unnamed_* values are KEPT (they may hold real
# data under a missing header).
_UNNAMED_RE = re.compile(r"^\s*unnamed[\s_:\-]*\d*\s*$", re.IGNORECASE)


def _is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() in ("", "null", "none", "nan", "n/a"):
        return True
    if isinstance(v, (list, dict)) and len(v) == 0:
        return True
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


_storage_client = None


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
        entry = {
            "name": blob.name,
            "size_bytes": blob.size,
            "loaded": False,
            "is_tech_spec": _is_tech_spec(blob.name),
        }

        try:
            raw = blob.download_as_bytes()
            text = raw.decode("utf-8", errors="replace")

            # Parsed files are expected to be JSON; fall back to raw text.
            if blob.name.lower().endswith(".json"):
                try:
                    parsed = json.loads(text)
                    parsed, removed = _prune_empty(parsed)
                    if removed:
                        entry["pruned_empty_fields"] = removed
                    documents[blob.name] = parsed
                except json.JSONDecodeError:
                    documents[blob.name] = text
            else:
                documents[blob.name] = text

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

    tool_context.state["opportunity_id"] = opportunity_id
    tool_context.state["template_date"] = date.today().isoformat()
    tool_context.state["opportunity_folder"] = matched_folder
    tool_context.state["opportunity_prefix"] = prefix  # full GCS prefix for save/load
    tool_context.state["opportunity_data"] = documents
    tool_context.state["opportunity_file_manifest"] = manifest
    tool_context.state["tech_spec_files"] = tech_spec_files
    tool_context.state["vbop_files"] = vbop_files
    tool_context.state["existing_validation_status"] = existing_status

    return {
        "status": "success",
        "opportunity_id": opportunity_id,
        "matched_folder": matched_folder,
        "tech_spec_files": tech_spec_files or "WARNING: no tech spec document found in this folder",
        "vbop_files": vbop_files or "WARNING: no VBOP file (named by opportunity id) found in this folder",
        "existing_validation_status": existing_status or "none - first run for this opportunity",
        "files": manifest,
        "documents": documents,
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
