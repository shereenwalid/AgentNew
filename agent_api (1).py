"""FastAPI wrapper around the quote-to-order ADK agent."""

import asyncio
import json
import os
import re
import traceback

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

AGENT_MOCK = os.getenv("AGENT_MOCK", "0") == "1"

# The LLM proxy sometimes glitches with:
#   "responsible AI check not passed for content-moderation-Health with score of ..."
# On that error we retry: send a plain "hi" to the model first (fresh, benign
# turn to reset the moderation state), then re-run the workflow with the
# opportunity id in a brand-new session. Default: 3 retries (4 attempts total).
RAI_MAX_RETRIES = int(os.getenv("RAI_MAX_RETRIES", "3"))
_RAI_PATTERNS = ("responsible ai", "content-moderation", "content moderation", "rai check")

# Transient capacity/rate errors from the model backend (Vertex/proxy).
OVERLOAD_MAX_RETRIES = int(os.getenv("OVERLOAD_MAX_RETRIES", "5"))
_OVERLOAD_PATTERNS = (
    "resource_exhausted", "resource exhausted", "429",
    "overloaded", "queue_preempted", "prefill_queue_preempted",
    "too many retries", "unavailable", "try again later",
)


def _is_overloaded_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _OVERLOAD_PATTERNS)


def _is_rai_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _RAI_PATTERNS)


_SIZE_PATTERNS = (
    "constraint is too tall", "constraint-is-too-big", "invalid_argument",
    "request contains an invalid argument", "too large", "exceeds",
)


def _is_size_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _SIZE_PATTERNS)


# How many times to shrink the condense budget and re-run on a size error,
# and the shrink factor each time.
SIZE_MAX_RETRIES = int(os.getenv("SIZE_MAX_RETRIES", "4"))
SIZE_SHRINK_FACTOR = float(os.getenv("SIZE_SHRINK_FACTOR", "0.5"))
SIZE_MIN_BUDGET = int(os.getenv("SIZE_MIN_BUDGET", "12000"))


async def _say_hi():
    """Benign warm-up turn straight to the model (not through the workflow)."""
    try:
        from vf_quote_to_order_agent_configured.Agent.agent import client, gemini_model
        await client.aio.models.generate_content(
            model=gemini_model.model, contents="hi"
        )
    except Exception:  # noqa: BLE001 - warm-up is best effort
        pass


# ── Detect which flavour of agent we have ───────────────────────────
# Mirrors the RA agent_server.py pattern: if the agent module exposes a
# plain `run_agent(query)` function, call that directly (e.g. a stub or a
# non-ADK implementation). Otherwise, if it exposes `root_agent`, drive it
# through the standard ADK Runner + InMemorySessionService.
_AGENT_MODULE = None
_AGENT_MODE = "not-loaded"
_run_agent_fn = None
_root_agent = None
_runner = None
_session_service = None

if not AGENT_MOCK:
    try:
        import vf_quote_to_order_agent_configured.Agent.agent as _AGENT_MODULE
    except Exception as _exc:  # noqa: BLE001
        _AGENT_MODULE = None
        _AGENT_MODE = f"import-error: {_exc}"

    if _AGENT_MODULE is not None:
        _run_agent_fn = getattr(_AGENT_MODULE, "run_agent", None)
        _root_agent = getattr(_AGENT_MODULE, "root_agent", None)

        if _root_agent is not None and _run_agent_fn is None:
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService

            _APP_NAME = "q2o"
            _session_service = InMemorySessionService()
            _runner = Runner(agent=_root_agent, app_name=_APP_NAME,
                              session_service=_session_service)
            _AGENT_MODE = f"adk:{getattr(_root_agent, 'name', 'root_agent')}"
        elif _run_agent_fn is not None:
            _AGENT_MODE = "function:run_agent"
        else:
            _AGENT_MODE = "error: agent.py must define run_agent(query) or root_agent"

app = FastAPI(title="Q2O Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory result cache: opp_id -> result dict
RESULTS: dict = {}

REQUIRED_LABELS = {
    "R1_po_signed_contract": "PO / signed contract",
    "R2_quote_to_customer": "Quote to customer",
    "R3_vendor_quote_clear_instruction": "Quote from vendor and clear instruction what vendor to order from",
    "R4_site_id": "Site ID",
    "R5_site_contact_info": "Site contact information",
    "R6_summary_sold_designed_hld": "Summary of what has been sold and/or Design / HLD if Solution Design involved",
    "R7_summary_content_complete": "Summary to include: services supplied, deployment, engineering asks/requirements, leadtime",
}

CHECK_LABELS = {
    "C1_leadtime_aligned_kpis": "If Leadtime provided to customer, is it aligned to our Product KPIs",
    "C2_circuit_order_form_attached": "Is the Circuit Order Form attached?",
    "C3_po_quote_attached_signed": "Is the PO/Quote attached and is it signed?",
    "C4_vendor_clear": "Is it clear what vendor the order is for?",
    "C5_third_party_vendor_docs": "Third Party usage: (Eir Fibre, Eir UG, EIL and SAB, Enet, Siro, Ripplecom/Host) quotes attached?",
    "C6_infra_orders_product_speed": "Are the correct Infra orders attached (Sales Enablement) and are they correct regarding product and speed etc?",
    "C7_site_info_correct": "Is the site information correct Site ID, Site Name, Site Address?",
    "C8_site_contact_details": "Is the site contact details provided, site contact name, email and site contact mobile number?",
    "C9_summary_matches_design_form": "Does the summary/design of the order match the design and order form attached?",
    "C10_correct_customer_account": "Are they on the correct customer account/company name so they are to be billed correctly?",
    "C11_lan_ip_for_dia": "Has the customers LAN IP details been provided for DIA?",
    "C12_public_ip_range_approved": "Customer Public address range(s) request details - has this been approved?",
    "C13_standard_offering_slash30": "Customer requesting IP address ranges from customers - standard offering is /30",
    "C14_kit_router_fulfilment": "Is the Kit correct, is the router compatible to the product being ordered and is the fulfilment run rate or ship back correct?",
    "C15_power_antenna_cables_licences": "Are all the power supplies, antenna's, extension cables, Licences requested, on the order?",
    "C16_third_party_install_in_quote": "Is the 3rd party installation included in the quote?",
    "C17_contract_term_matches": "Is the contract term provided and does it match the order?",
    "C18_bend_info_correct": "Has the B-End information if required been filled out and is it correct?",
    "C19_qos_af_ef_correct": "Has the QoS (Quality of Service) if required been filled out and is it correct? AF and EF % On order form",
    "C20_cease_details_included": "If services are to be ceased after the new service is completed, the cease details must be added to the request.",
    "C21_upgrade_downgrade_stated": "If the order is an Upgrade/Downgrade this must be clearly stated on the order.",
    "C22_referenced_files_attached": "If there are any comments to please refer to a certain email or file, please make sure it is attached.",
    "C23_vendor_quote_still_valid": "Is the vendor quote still valid (not out of date)?",
    "C24_vf_quote_to_customer_attached": "Is the VF Quote to Customer attached?",
    "C25_leadtime_indicated_to_customer": "What Leadtime (if any) have you indicated to the customer",
    "C26_vendor_quotations_attached": "Are the Vendor Quotations attached?",
    "C27_tech_spec_commercial_approval_margin": "Tech spec includes commercial approval incl. margin (pricing tool / EDRA / commercial manager email)?",
}


BASE_CHECK_LABELS = {
    "B1_customer_name_in_bsp": "Customer name exists / matches in BSP (else raise MDG)",
    "B2_delivery_address_in_bsp": "Delivery address exists in BSP (else raise BSP Team)",
    "B3_billing_entity_matches": "Billing entity name matches bill_to_name in BSP (else raise MDG)",
    "B4_opp_ref_matches_vbop": "Opportunity ref in tech spec matches VBOP",
    "B5_po_number_consistent": "PO number consistent across VBOP / customer PO (or email confirmation)",
    "B6_cost_less_than_sell": "Vendor PO cost price < sell price (Vodafone quote & customer PO)",
    "B7_product_codes_in_docs": "Every tech spec product code present in vendor quote (or name found)",
    "B8_product_codes_in_bsp_mdm": "Every tech spec product code exists in BSP via MDM (else raise MDM)",
    "B9_quantities_align": "Quantities align across all files (vendor quote may be higher)",
    "B10_vendor_quote_not_expired": "Vendor quote still valid (within 30 days, not expired)",
    "B11_contact_details_complete": "Contact name & details in tech spec are complete",
    "B12_order_readiness": "Order readiness - aggregate of all base checks",
}

CIRCUIT_CHECK_LABELS = {
    "X1_contract_term_present": "Contract term present (circuit order form / tech spec)",
    "X2_premise_id_siro": "Premise ID present (required for SIRO vendors)",
    "X3_service_type_matches": "Service type present and matches supporting docs",
    "X4_speed_matches": "Speed present and matches customer order form",
    "X5_product_name_type_exist": "Product name and type exist",
    "X6_service_address_in_bsp": "Service address present and matches BSP (else raise BSP Team)",
    "X7_po_matches_vbop": "PO number in circuit order form matches VBOP",
    "X8_billing_freq_aligned": "Billing frequency present and aligned",
    "X9_po_type_recurring": "PO type set to Recurring",
}

def _parse_json_block(text):
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# Checks that only apply to CIRCUIT orders. For non-circuit (e.g. Kit) orders
# these are dropped from the response entirely, so the UI never shows them and
# they never count towards failures.
CIRCUIT_ONLY_KEYS = {
    "R4_site_id",                      # Site ID - circuit orders only
    "C2_circuit_order_form_attached",  # Circuit Order Form
    "C5_third_party_vendor_docs",      # eir / enet / SIRO / Ripplecom quotes
    "C6_infra_orders_product_speed",   # infra orders, product & speed
    "C11_lan_ip_for_dia",              # DIA LAN IP
    "C12_public_ip_range_approved",    # public IP ranges
    "C13_standard_offering_slash30",   # /30 standard offering
    "C18_bend_info_correct",           # B-End: EIL, SAB, eir exchange, enet NNI
    "C19_qos_af_ef_correct",           # QoS AF / EF %
}


def _feedback_index(saved_status):
    """Build {check_key: feedback_record} from a previously saved
    validation_status.json (any of its check sections)."""
    idx = {}
    if not isinstance(saved_status, dict):
        return idx
    for section in ("criteria", "checks", "base_checks", "circuit_checks"):
        for c in saved_status.get(section, []) or []:
            if isinstance(c, dict) and c.get("key") and c.get("feedback"):
                idx[c["key"]] = c["feedback"]
    return idx


def _shape_result(opp_id, validation, order, saved_status=None):
    """Convert agent output into the UI shape.

    saved_status: the previously saved validation_status.json (if any), used to
    re-attach human feedback so it survives re-runs and is visible to everyone.
    """
    v = _parse_json_block(validation) or {}
    o = _parse_json_block(order) or {}
    fb_idx = _feedback_index(saved_status)
    # The extractor no longer uses a constrained output_schema (it produced a
    # decoding state machine too large for the serving layer). Validate/coerce
    # the parsed JSON against BSPOrderTemplate here instead - best effort, so a
    # minor deviation never blocks the result.
    try:
        from vf_quote_to_order_agent_configured.Agent.models import BSPOrderTemplate  # type: ignore
        if isinstance(o, dict):
            o = BSPOrderTemplate(**o).model_dump()
    except Exception:  # noqa: BLE001 - keep the raw parsed dict if coercion fails
        pass

    def items(section, labels):
        out = []
        data = v.get(section, {}) or {}

        # The agent now returns compact arrays to save tokens:
        #   ["C1", "Y|N", "reason", "action?"]  (action only for B*/X*)
        # It maps by the SHORT id (C1) or the full key (C1_leadtime_...).
        # We still support the legacy object format for safety.
        short_index = {}   # short id -> (result, notes, action)
        if isinstance(data, list):
            for row in data:
                if not row:
                    continue
                key = str(row[0]).strip()
                res = str(row[1]).strip().upper() if len(row) > 1 else "N"
                note = row[2] if len(row) > 2 else ""
                action = row[3] if len(row) > 3 else "none"
                short_index[key.upper()] = (res, note, action)

        for key, label in labels.items():
            short = key.split("_", 1)[0].upper()   # "C1_leadtime..." -> "C1"
            if short_index:
                res_t, note, action = short_index.get(short, ("N", "", "none"))
                res = str(res_t).strip().upper()
                entry_feedback = None
            else:
                entry = data.get(key, {}) or {}
                res = str(entry.get("result", "N")).strip().upper()
                note = entry.get("notes", "")
                action = entry.get("action", "none") or "none"
                entry_feedback = entry.get("feedback")
            # Saved feedback (from a previous run) always wins so it persists.
            if key in fb_idx:
                entry_feedback = fb_idx[key]
            out.append({
                "key": key,
                "label": label,
                "ok": res == "Y",
                "notes": note or ("Not verified" if res != "Y" else ""),
                "action": action or "none",
                "feedback": entry_feedback,
            })
        return out

    order_category = str(v.get("order_category") or o.get("order_category") or "").strip()
    is_circuit = order_category.lower() == "circuit" or ("circuit_checks" in v)

    criteria = items("required_information", REQUIRED_LABELS)
    checks = items("checks", CHECK_LABELS)
    base_checks = items("bsp_base_checks", BASE_CHECK_LABELS)
    circuit_checks = items("circuit_checks", CIRCUIT_CHECK_LABELS) if is_circuit else []

    # Non-circuit orders: drop circuit-specific checks entirely.
    if not is_circuit:
        criteria = [c for c in criteria if c["key"] not in CIRCUIT_ONLY_KEYS]
        checks = [c for c in checks if c["key"] not in CIRCUIT_ONLY_KEYS]

    failed = [i for i in criteria + checks + base_checks + circuit_checks if not i["ok"]]

    return {
        "status": "ok",
        "opportunity_id": opp_id,
        "matched_folder": v.get("matched_folder", ""),
        "order_category": order_category or "Not Specified",
        "is_circuit": is_circuit,
        "criteria": criteria,
        "checks": checks,
        "base_checks": base_checks,
        "circuit_checks": circuit_checks,
        "failed_count": len(failed),
        "overall": v.get("overall", {}),
        "order": o,
        "raw_validation": v,
    }


_MOCK_BASE_FAIL = {"B1_customer_name_in_bsp": "raise_mdg", "B8_product_codes_in_bsp_mdm": "raise_mdm"}
_MOCK_CIRCUIT_FAIL = {"X2_premise_id_siro": "raise_bsp"}

MOCK_VALIDATION = {
    "matched_folder": "4234-opp-DEMO",
    "order_category": "Circuit",
    "required_information": {k: {"result": ("N" if k == "R4_site_id" else "Y"),
                                 "notes": ("Missing required value" if k == "R4_site_id" else "Found in documents")}
                             for k in REQUIRED_LABELS},
    "checks": {k: {"result": ("N" if k == "C2_circuit_order_form_attached" else "Y"),
                   "notes": ("Document not found in payload" if k == "C2_circuit_order_form_attached" else "Verified")}
               for k in CHECK_LABELS},
    "bsp_base_checks": {k: {"result": ("N" if k in _MOCK_BASE_FAIL else "Y"),
                            "notes": ("No acceptable match in BSP" if k in _MOCK_BASE_FAIL else "Matched in BSP"),
                            "action": _MOCK_BASE_FAIL.get(k, "none")}
                        for k in BASE_CHECK_LABELS},
    "circuit_checks": {k: {"result": ("N" if k in _MOCK_CIRCUIT_FAIL else "Y"),
                           "notes": ("SIRO premise id missing" if k in _MOCK_CIRCUIT_FAIL else "Present and matches"),
                           "action": _MOCK_CIRCUIT_FAIL.get(k, "none")}
                       for k in CIRCUIT_CHECK_LABELS},
    "overall": {"ready_for_order": "N",
                "blocking_issues": ["Site ID missing", "Circuit Order Form not attached",
                                    "Customer not in BSP", "Product code not in MDM", "SIRO premise id missing"],
                "needs_human_review": []},
}

MOCK_ORDER = {
    "order_title": "Enterprise_New_eir_DIA_TechCorp_SITE-12345",
    "order_type": "New", "order_category": "Circuit",
    "external_reference_number": "DEMO",
    "company_code": "VF-IE-001", "bill_to_party": "TechCorp Ltd", "ship_to_party": "TechCorp Ltd",
    "line_items": [
        {"quantity": 1, "item": "Dedicated Internet Access 100Mbps", "sku": "TEL-DIA-100",
         "location": "TechCorp Ltd", "price": "450.00", "charge_type": "Recurring",
         "recurring_period": "Monthly", "fulfilment": "DIA-100-S"},
        {"quantity": 1, "item": "SLA", "sku": "SLA", "location": "TechCorp Ltd",
         "price": "0.00", "charge_type": "Recurring", "recurring_period": "Monthly", "fulfilment": "SLA"},
        {"quantity": 1, "item": "ECS-INSTALL", "sku": "ECS-INSTALL", "location": "TechCorp Ltd",
         "price": "350.00", "charge_type": "One Off", "recurring_period": "Not Applicable", "fulfilment": "H202"},
        {"quantity": 1, "item": "ECS-INTSAndConfigIRL", "sku": "ECS-INTSAndConfigIRL", "location": "TechCorp Ltd",
         "price": "500.00", "charge_type": "One Off", "recurring_period": "Not Applicable", "fulfilment": "H202"},
    ],
}


async def _run_agent(opp_id: str):
    """Run the workflow (in whichever mode was detected) and return
    (validation_report, bsp_order). A brand-new session is created on every
    call, so calling this again after `_say_hi()` naturally starts fresh."""
    if _AGENT_MODE.startswith("adk:"):
        import uuid

        from google.genai import types

        session = await _session_service.create_session(
            app_name="q2o", user_id="ui", session_id=str(uuid.uuid4())
        )
        msg = types.Content(role="user", parts=[types.Part(text=f"{opp_id}")])

        async for _event in _runner.run_async(
            user_id="ui", session_id=session.id, new_message=msg
        ):
            pass

        session = await _session_service.get_session(
            app_name="q2o", user_id="ui", session_id=session.id
        )
        state = session.state or {}
        return (state.get("validation_report"), state.get("bsp_order"),
                state.get("opportunity_prefix", ""),
                state.get("existing_validation_status"))

    if _AGENT_MODE == "function:run_agent":
        result = _run_agent_fn(opp_id)
        if asyncio.iscoroutine(result):
            result = await result
        # Expect run_agent to return either (validation, order) or a single
        # JSON-ish payload containing both keys.
        if isinstance(result, tuple) and len(result) == 2:
            return result[0], result[1], "", None
        parsed = _parse_json_block(result) or {}
        return parsed.get("validation_report"), parsed.get("bsp_order"), "", None

    raise RuntimeError(f"Agent not available ({_AGENT_MODE})")


@app.get("/health")
def health():
    return {"ok": True, "mock": AGENT_MOCK, "mode": _AGENT_MODE, "rai_max_retries": RAI_MAX_RETRIES}


@app.post("/process/{opp_id}")
async def process(opp_id: str):
    try:
        if AGENT_MOCK:
            await asyncio.sleep(4)
            result = _shape_result(opp_id, MOCK_VALIDATION, MOCK_ORDER)
        else:
            attempt = 0
            overload_attempt = 0
            size_attempt = 0
            while True:
                try:
                    validation, order, gcs_prefix, saved_status = await _run_agent(opp_id)
                    break
                except Exception as exc:  # noqa: BLE001
                    # Input too big for the model: shrink the condense budget and re-run.
                    # get_opportunity_data reads CONDENSE_TOTAL_BUDGET live from env,
                    # so lowering it here makes the next run send a smaller payload.
                    if _is_size_error(exc) and size_attempt < SIZE_MAX_RETRIES:
                        size_attempt += 1
                        cur = int(os.getenv("CONDENSE_TOTAL_BUDGET", "120000"))
                        new_budget = max(SIZE_MIN_BUDGET, int(cur * SIZE_SHRINK_FACTOR))
                        os.environ["CONDENSE_TOTAL_BUDGET"] = str(new_budget)
                        print(f"[size retry {size_attempt}/{SIZE_MAX_RETRIES}] input too big - "
                              f"shrinking payload budget {cur} -> {new_budget} and re-running")
                        continue
                    # Transient capacity/429: back off and retry (model queue overloaded)
                    if _is_overloaded_error(exc) and overload_attempt < OVERLOAD_MAX_RETRIES:
                        overload_attempt += 1
                        wait = min(2 ** overload_attempt, 30)  # 2,4,8,16,30s
                        print(f"[overload retry {overload_attempt}/{OVERLOAD_MAX_RETRIES}] "
                              f"429/RESOURCE_EXHAUSTED - backing off {wait}s then retrying")
                        await asyncio.sleep(wait)
                        continue
                    if _is_rai_error(exc) and attempt < RAI_MAX_RETRIES:
                        attempt += 1
                        print(f"[RAI retry {attempt}/{RAI_MAX_RETRIES}] {exc} "
                              f"- sending 'hi' then re-sending opportunity id "
                              f"in a fresh session")
                        await _say_hi()
                        await asyncio.sleep(1)
                        continue
                    raise
            result = _shape_result(opp_id, validation, order, saved_status=saved_status)
            result["gcs_prefix"] = gcs_prefix
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        msg = str(exc)
        if _is_size_error(exc):
            msg = ("The opportunity documents were too large for the model even after "
                   "automatic compaction. Lower CONDENSE_TOTAL_BUDGET further, or the "
                   "biggest file needs cleaning at source. "
                   f"[{msg[:200]}]")
        elif _is_overloaded_error(exc):
            msg = ("The model service is temporarily overloaded (429 / RESOURCE_EXHAUSTED). "
                   "This is a transient capacity issue - please try again in a moment. "
                   f"[{msg[:200]}]")
        result = {"status": "error", "opportunity_id": opp_id, "message": msg,
                  "criteria": [], "checks": [], "failed_count": -1, "order": {}}
    RESULTS[opp_id] = result
    return result


@app.get("/result/{opp_id}")
def result(opp_id: str):
    return RESULTS.get(opp_id, {"status": "missing", "opportunity_id": opp_id})

class FeedbackItem(BaseModel):
    check_key: str
    option: str            # raise_bsp | raise_mdm | raise_mdg | manual_correction | raise_sales | ...
    comment: str = ""
    by: str = "ui"


@app.post("/feedback/{opp_id}")
async def save_feedback(opp_id: str, item: FeedbackItem):
    """Attach human feedback to a check and persist the whole status to the
    opportunity's GCS folder (validation_status.json), so re-runs and other
    users see it."""
    import datetime as _dt

    result = RESULTS.get(opp_id)
    if not result:
        return {"status": "error", "message": "No result in memory for this opportunity. Run /process first."}

    record = {
        "option": item.option,
        "comment": item.comment,
        "saved_date": _dt.datetime.utcnow().isoformat() + "Z",
        "by": item.by,
    }
    # attach to whichever section holds the key
    for section in ("criteria", "checks", "base_checks", "circuit_checks"):
        for c in result.get(section, []):
            if c.get("key") == item.check_key:
                c["feedback"] = record

    # Persist to GCS via the agent tool (skips in mock mode).
    if not AGENT_MOCK:
        try:
            import json as _json
            from google.adk.tools import ToolContext  # noqa: F401
            # Reuse the agent's save tool directly with a lightweight state shim.
            from vf_quote_to_order_agent_configured.Agent.tools import (  # type: ignore
                save_validation_status,
            )

            class _Ctx:
                def __init__(self, state):
                    self.state = state
            prefix = result.get("_prefix") or result.get("raw_validation", {}).get("_prefix")
            ctx = _Ctx({"opportunity_prefix": result.get("gcs_prefix", ""),
                        "opportunity_id": opp_id})
            if result.get("gcs_prefix"):
                save_validation_status(_json.dumps(result), ctx)
            else:
                RESULTS[opp_id] = result
                return {"status": "saved_memory_only",
                        "message": "No GCS prefix captured; feedback stored in memory only.",
                        "feedback": record}
        except Exception as exc:  # noqa: BLE001
            RESULTS[opp_id] = result
            return {"status": "saved_memory_only",
                    "message": f"Feedback stored in memory; GCS save failed: {exc}",
                    "feedback": record}

    RESULTS[opp_id] = result
    return {"status": "success", "opportunity_id": opp_id,
            "check_key": item.check_key, "feedback": record}
