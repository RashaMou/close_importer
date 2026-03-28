import argparse
import base64
import csv
import json
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def clean_phone(phone: str) -> str | None:
    """Normalize a phone number. A leading + is preserved (international format);
    all other non-digit characters are stripped. Returns None if no digits remain."""
    if not phone:
        return None
    normalized = re.sub(r"[^\d+]", "", phone.strip())
    leading_plus = normalized.startswith("+")
    digits = re.sub(r"\D", "", normalized)
    if not digits:
        return None
    return ("+" if leading_plus else "+1") + digits


def clean_email(email: str) -> str | None:
    """Basic email validation. Must have a local part, @, domain, and TLD.
    Only word characters, dots, hyphens, and plus signs are accepted in each part.
    Returns None if the email doesn't pass."""
    if not email:
        return None
    email = email.strip()
    if re.match(r"^[\w.+\-]+@[\w\-]+(\.[\w\-]+)*\.[a-zA-Z]{2,}$", email):
        return email
    return None


def split_values(raw: str) -> list[str]:
    """Split a cell that may contain multiple values separated by ;, ,, whitespace, or newlines."""
    if not raw:
        return []
    return [v.strip() for v in re.split(r"[;,\s]+", raw) if v.strip()]


def _merge_field(existing: dict, incoming: dict, key: str, id_field: str) -> bool:
    known = {e[id_field] for e in existing.get(key, [])}
    added = [e for e in incoming.get(key, []) if e[id_field] not in known]
    if added:
        existing.setdefault(key, []).extend(added)
    return bool(added)


def merge_contact(existing: dict, incoming: dict) -> bool:
    changed = _merge_field(existing, incoming, "emails", "email")
    changed |= _merge_field(existing, incoming, "phones", "phone")
    return changed


def clean_date(date_str: str) -> datetime | None:
    """Parse a date in D.M.YYYY format (with or without leading zeros).
    Returns a datetime object, or None if unparseable."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d.%m.%Y")
    except ValueError:
        return None


def clean_revenue(revenue_str: str) -> float | None:
    """Strip currency symbols and commas, then parse as float.
    Returns None if the value can't be parsed."""
    if not revenue_str:
        return None
    # Remove $, commas, spaces, and other common currency formatting
    cleaned = re.sub(r"[\$,\s]", "", revenue_str.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------


class ProgressBar:
    _SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str = ""):
        self.label = label.strip()
        self.current = 0

    def advance(self, n: int = 1) -> None:
        self.current += n
        spinner = self._SPINNER[self.current % len(self._SPINNER)]
        print(f"\r  {spinner} {self.label}  {self.current:,}", end="", flush=True)

    def done(self, summary: str = "") -> None:
        msg = f"\r  ✓ {self.label}  {self.current:,}"
        if summary:
            msg += f"  {summary}"
        print(msg)


# ---------------------------------------------------------------------------
# Close API helpers
# ---------------------------------------------------------------------------


def make_api_request(
    method: str, path: str, api_key: str, payload: dict | None = None
) -> tuple[dict | None, dict | None]:
    """Make an authenticated request to the Close API.

    The Close API uses HTTP Basic Auth where the API key is the username
    and the password is empty. We encode this as base64 per the spec.

    Returns (data, error): on success data is the parsed response and error is
    None; on failure data is None and error is the parsed error response.
    """
    url = f"https://api.close.com/api/v1{path}"

    # Encode API key for Basic Auth: base64("api_key:")
    credentials = base64.b64encode(f"{api_key}:".encode()).decode()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Basic {credentials}",
    }

    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read()), None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            error = json.loads(body)
        except json.JSONDecodeError:
            error = {"errors": [body]}
        return None, error
    except urllib.error.URLError as e:
        return None, {"errors": [str(e.reason)]}


def ensure_custom_fields(api_key: str, dry_run: bool = False) -> dict:
    """Ensure the 'custom.Company Founded' (date) and 'custom.Company Revenue' (number)
    custom fields exist on leads in this Close org.

    If they already exist, their IDs are returned.
    If they don't exist, they are created and their new IDs are returned.

    Returns a dict: { 'founded': 'cf_...', 'revenue': 'cf_...' }
    """
    if dry_run:
        print("  Would check for custom fields and create any that are missing.")
        return {}
    # Fetch all existing lead custom fields for this org
    print("  Fetching existing custom fields from Close...")
    response, _ = make_api_request("GET", "/custom_field/lead/", api_key)
    existing = response.get("data", []) if response else []

    custom_fields = {}  # { field_key: field_id }

    # Check which fields already exist by name
    for field in existing:
        if field["name"] == "Company Founded":
            custom_fields["founded"] = field["id"]
            print(f'  ✓ "Company Founded" already exists ({field["id"]})')
        elif field["name"] == "Company Revenue":
            custom_fields["revenue"] = field["id"]
            print(f'  ✓ "Company Revenue" already exists ({field["id"]})')

    # Create any fields that are missing
    if "founded" not in custom_fields:
        print('  Creating "Company Founded" custom field (type: date)...')
        result, err = make_api_request(
            "POST",
            "/custom_field/lead/",
            api_key,
            {
                "name": "Company Founded",
                "type": "date",  # Close expects YYYY-MM-DD strings for this type
            },
        )

        if result:
            custom_fields["founded"] = result["id"]
            print(f'  ✓ Created "Company Founded" ({result["id"]})')
        else:
            print(f'  ✗ Failed to create "Company Founded": {err}')

    if "revenue" not in custom_fields:
        print('  Creating "Company Revenue" custom field (type: number)...')
        result, err = make_api_request(
            "POST",
            "/custom_field/lead/",
            api_key,
            {
                "name": "Company Revenue",
                "type": "number",
            },
        )

        if result:
            custom_fields["revenue"] = result["id"]
            print(f'  ✓ Created "Company Revenue" ({result["id"]})')
        else:
            print(f'  ✗ Failed to create "Company Revenue": {err}')

    return custom_fields


# ---------------------------------------------------------------------------
# CSV parsing and lead grouping
# ---------------------------------------------------------------------------


def parse_csv(
    filepath: str, custom_fields: dict
) -> tuple[dict, list[dict], list[dict]]:
    """Read the CSV file and group rows into leads by company name.

    Each company becomes one lead with a list of contacts.
    Rows missing a company name are discarded.
    Invalid contact fields (email, phone) are dropped but the contact is kept
    as long as it has a name.

    Returns (leads, skipped, invalid_values) where leads is a dict keyed by
    company name, skipped is a list of rows that were dropped entirely, and
    invalid_values is a list of individual field values that were discarded.
    """
    leads = {}  # { company_name: lead_dict }
    skipped = []  # { "row": str, "reason": str }
    invalid_values = []  # { "who": str, "value": str, "reason": str }

    expected_headers = {
        "Company",
        "Contact Name",
        "Contact Emails",
        "Contact Phones",
        "custom.Company Founded",
        "custom.Company Revenue",
        "Company US State",
    }

    print(f"  Reading {filepath}. Validating headers...")

    try:
        f_handle = open(filepath, newline="", encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"CSV file not found: {filepath}")
    with f_handle as f:
        reader = csv.DictReader(f)
        actual_headers = set(reader.fieldnames or [])

        # --- Validate headers ---
        missing = expected_headers - actual_headers
        extra = actual_headers - expected_headers

        if missing or extra:
            lines = ["CSV headers do not match expected layout."]
            if missing:
                lines.append(f"  Missing: {', '.join(sorted(missing))}")
            if extra:
                lines.append(f"  Unexpected: {', '.join(sorted(extra))}")
            lines.append(f"  Expected: {', '.join(sorted(expected_headers))}")
            raise ValueError("\n".join(lines))

        bar = ProgressBar(label="Parsing CSV")

        for row in reader:
            bar.advance()
            company = row.get("Company", "").strip()

            # Hard requirement: company name must exist
            if not company:
                skipped.append(
                    {"row": f"Row {reader.line_num}", "reason": "missing company name"}
                )
                continue

            # --- Validate and clean contact fields ---
            contact_name = row.get("Contact Name", "").strip() or None
            who = f'"{company}"' + (f' : "{contact_name}"' if contact_name else "")

            emails = []
            for raw in split_values(row.get("Contact Emails", "")):
                cleaned = clean_email(raw)
                if cleaned:
                    emails.append(cleaned)
                else:
                    invalid_values.append(
                        {"who": who, "value": raw, "reason": "invalid email"}
                    )

            phones = []
            for raw in [
                v.strip()
                for v in re.split(r"[;,\n]+", row.get("Contact Phones", ""))
                if v.strip()
            ]:
                if not re.search(r"\d", raw):
                    continue  # decorative token (e.g. emoji), not a phone attempt
                cleaned = clean_phone(raw)
                if cleaned:
                    phones.append(cleaned)
                else:
                    invalid_values.append(
                        {"who": who, "value": raw, "reason": "invalid phone"}
                    )

            # --- Validate and clean custom fields ---
            founded = clean_date(row.get("custom.Company Founded", ""))
            revenue = clean_revenue(row.get("custom.Company Revenue", ""))
            state = row.get("Company US State", "").strip() or None

            # --- Build the contact object. Only include fields that have values ---
            contact = {}
            if contact_name:
                contact["name"] = contact_name
            if emails:
                contact["emails"] = [{"type": "office", "email": e} for e in emails]
            if phones:
                contact["phones"] = [{"type": "office", "phone": p} for p in phones]

            # If this company already has a lead entry, merge or discard.
            # Otherwise, create a new lead entry.
            if company in leads:
                existing = leads[company]
                changed = False

                # --- Merge contact ---
                # Match against an existing contact by name. If none matches, add as new.
                if contact:
                    incoming_name = contact.get("name") or ""
                    matched = next(
                        (
                            c
                            for c in existing["contacts"]
                            if (c.get("name") or "") == incoming_name
                        ),
                        None,
                    )
                    if matched:
                        changed = merge_contact(matched, contact) or changed
                    else:
                        existing["contacts"].append(contact)
                        changed = True

                # --- Merge lead-level fields (fill in only if currently missing) ---
                if not existing["_founded"] and founded:
                    existing["_founded"] = founded
                    if "founded" in custom_fields:
                        existing[f'custom.{custom_fields["founded"]}'] = (
                            founded.strftime("%Y-%m-%d")
                        )
                    changed = True

                if existing["_revenue"] is None and revenue is not None:
                    existing["_revenue"] = revenue
                    if "revenue" in custom_fields:
                        existing[f'custom.{custom_fields["revenue"]}'] = revenue
                    changed = True

                if not existing["_state"] and state:
                    existing["_state"] = state
                    existing["addresses"] = [
                        {"label": "business", "state": state, "country": "US"}
                    ]
                    changed = True

                if not changed:
                    skipped.append(
                        {
                            "row": f"Row {reader.line_num}",
                            "reason": "exact duplicate of an earlier row",
                        }
                    )
            else:
                # Start a new lead. Store metadata alongside the API payload
                lead = {
                    "name": company,
                    "contacts": [contact] if contact else [],
                    # These are kept for date filtering and reporting only:
                    "_founded": founded,
                    "_revenue": revenue,
                    "_state": state,
                }
                if state:
                    lead["addresses"] = [
                        {"label": "business", "state": state, "country": "US"}
                    ]

                # Add custom fields to the lead payload using the org's field IDs.
                # Close expects date fields as YYYY-MM-DD strings, and revenue as a number.
                if founded and "founded" in custom_fields:
                    lead[f'custom.{custom_fields["founded"]}'] = founded.strftime(
                        "%Y-%m-%d"
                    )

                if revenue is not None and "revenue" in custom_fields:
                    lead[f'custom.{custom_fields["revenue"]}'] = revenue

                leads[company] = lead

        bar.done(f"→ {len(leads)} leads, {len(skipped)} skipped")
    return leads, skipped, invalid_values


# ---------------------------------------------------------------------------
# Lead import (POST / PUT to Close API)
# ---------------------------------------------------------------------------

_INTERNAL_KEYS = {"_founded", "_revenue", "_state"}


def find_close_lead_by_name(name: str, api_key: str) -> tuple[dict | None, str | None]:
    """Search Close for an existing lead with exactly this name.
    Returns (lead, None) if found, (None, None) if not found, or (None, error) on failure.
    """
    query = urllib.parse.urlencode({"query": f'name:"{name}"', "_limit": 1})
    result, error = make_api_request("GET", f"/lead/?{query}", api_key)
    if error:
        return None, json.dumps(error)
    if result and result.get("data"):
        return result["data"][0], None
    return None, None


def build_lead_update(existing: dict, incoming: dict) -> dict:
    """Return fields from incoming that are absent or empty in existing.
    Contacts are excluded — they are handled separately by sync_contacts."""
    update = {}
    for key, value in incoming.items():
        if key == "contacts":
            continue
        existing_val = existing.get(key)
        if (existing_val is None or existing_val == [] or existing_val == "") and value:
            update[key] = value
    return update


def sync_contacts(
    lead_id: str,
    existing_contacts: list[dict],
    incoming_contacts: list[dict],
    api_key: str,
    dry_run: bool = False,
) -> list[str]:
    """Add new contacts to an existing lead and fill in missing emails/phones
    on contacts that already exist (matched by name).
    Returns a list of human-readable change descriptions (empty = no changes)."""
    existing_by_name = {(c.get("name") or ""): c for c in existing_contacts}
    changes = []
    for incoming in incoming_contacts:
        incoming_name = incoming.get("name") or ""
        display_name = f'"{incoming_name}"' if incoming_name else "(no name)"
        matched = existing_by_name.get(incoming_name)

        if not matched:
            if not dry_run:
                make_api_request(
                    "POST", "/contact/", api_key, {**incoming, "lead_id": lead_id}
                )
            changes.append(f"new contact {display_name}")
        else:
            update = {}

            # Can't reuse _merge_field here: we need phone normalization on the
            # Close side (Close stores phones as-entered, e.g. "+1 123"), and we
            # build a separate update dict rather than mutating matched in place.
            existing_emails = {e["email"] for e in matched.get("emails", [])}
            new_emails = [
                e
                for e in incoming.get("emails", [])
                if e["email"] not in existing_emails
            ]
            if new_emails:
                update["emails"] = matched["emails"] + new_emails
                changes.append(
                    f"contact {display_name}: +{len(new_emails)} email(s) {[e['email'] for e in new_emails]} (Close has: {sorted(existing_emails)})"
                )

            existing_phones_raw = {p["phone"] for p in matched.get("phones", [])}
            existing_phones_normalized = {
                clean_phone(p["phone"]) for p in matched.get("phones", [])
            }
            new_phones = [
                p
                for p in incoming.get("phones", [])
                if p["phone"] not in existing_phones_normalized
            ]
            if new_phones:
                update["phones"] = matched["phones"] + new_phones
                changes.append(
                    f"contact {display_name}: +{len(new_phones)} phone(s) {[p['phone'] for p in new_phones]} (Close has: {sorted(existing_phones_raw)})"
                )

            if update and not dry_run:
                make_api_request("PUT", f"/contact/{matched['id']}/", api_key, update)

    return changes


def import_leads(
    leads: dict, api_key: str, dry_run: bool = False
) -> tuple[int, int, int, list[dict]]:
    """Upsert each lead into Close: create if new, update if existing.
    In dry-run mode, Close is queried for duplicates but nothing is written.
    Data is expected to be fully validated before this point.
    Returns (created, updated, unchanged, failures)."""
    total = len(leads)

    if not total:
        print("  No leads to import.")
        return 0, 0, 0, []

    label = "Checking leads" if dry_run else "Syncing leads"
    bar = ProgressBar(label=label)

    created = updated = unchanged = 0
    failures = []
    pending_updates = []  # collected for dry-run display after bar completes

    for lead in leads.values():
        # Build copy of lead dict without the internal keys
        payload = {k: v for k, v in lead.items() if k not in _INTERNAL_KEYS}

        try:
            existing, search_error = find_close_lead_by_name(lead["name"], api_key)

            if search_error:
                failures.append(
                    {
                        "lead": lead["name"],
                        "error": f"Could not search Close for existing lead: {search_error}",
                    }
                )
                continue

            if not existing:
                if not dry_run:
                    result, error = make_api_request("POST", "/lead/", api_key, payload)
                    if not result:
                        failures.append(
                            {
                                "lead": lead["name"],
                                "error": (
                                    json.dumps(error) if error else "Unknown error"
                                ),
                            }
                        )
                        continue
                created += 1
            else:
                lead_update = build_lead_update(existing, payload)

                if lead_update and not dry_run:
                    result, error = make_api_request(
                        "PUT", f"/lead/{existing['id']}/", api_key, lead_update
                    )
                    if not result:
                        failures.append(
                            {
                                "lead": lead["name"],
                                "error": (
                                    json.dumps(error) if error else "Unknown error"
                                ),
                            }
                        )
                        continue

                contact_changes = sync_contacts(
                    existing["id"],
                    existing.get("contacts", []),
                    payload.get("contacts", []),
                    api_key,
                    dry_run=dry_run,
                )

                if lead_update or contact_changes:
                    updated += 1
                    if dry_run:
                        lead_debug = {
                            k: (existing.get(k), lead_update[k]) for k in lead_update
                        }
                        pending_updates.append(
                            (lead["name"], lead_debug, contact_changes)
                        )
                else:
                    unchanged += 1
        finally:
            bar.advance()

    if dry_run:
        bar.done(
            f"→ {created} to create, {updated} to update, {unchanged} unchanged, {len(failures)} failed"
        )
    else:
        bar.done(
            f"→ {created} created, {updated} updated, {unchanged} unchanged, {len(failures)} failed"
        )

    if pending_updates:
        print()
        for name, lead_debug, contact_changes in pending_updates:
            print(f'  Would update "{name}":')
            for key, (close_val, csv_val) in lead_debug.items():
                print(f"    • {key}: Close={close_val!r}  CSV={csv_val!r}")
            for desc in contact_changes:
                print(f"    • {desc}")

    return created, updated, unchanged, failures


# ---------------------------------------------------------------------------
# Filtering, segmentation, and report generation
# ---------------------------------------------------------------------------


def filter_by_founded(
    leads: dict, start: datetime | None, end: datetime | None
) -> dict:
    """Return leads whose _founded date falls within [start, end] (inclusive).
    If neither bound is given, all leads are returned."""
    if start is None and end is None:
        return leads
    return {
        name: lead
        for name, lead in leads.items()
        if lead["_founded"] is not None
        and lead["_founded"] >= start
        and lead["_founded"] <= end
    }


def segment_by_state(leads: dict) -> list[dict]:
    """Group leads by US state and compute per-state stats.
    Leads without a state are excluded.
    Returns rows sorted alphabetically by state, ready for CSV output."""
    groups: dict[str, list] = {}
    for lead in leads.values():
        state = lead.get("_state")
        if state:
            groups.setdefault(state, []).append(lead)

    rows = []
    for state in sorted(groups):
        state_leads = groups[state]
        revenues = []
        top_lead = state_leads[0]
        for lead in state_leads:
            rev = lead["_revenue"] or 0
            revenues.append(rev)
            if rev > (top_lead["_revenue"] or 0):
                top_lead = lead
        rows.append(
            {
                "US State": state,
                "Total number of leads": len(state_leads),
                "The lead with most revenue": top_lead["name"],
                "Total revenue": f"${sum(revenues):,.2f}",
                "Median revenue": f"${statistics.median(revenues):,.2f}",
            }
        )
    return rows


def write_state_report(
    rows: list[dict], output_path: str, dry_run: bool = False
) -> None:
    """Write the state-segmented report to a CSV file.
    In dry-run mode, prints a preview instead of writing."""
    if not rows:
        print("  No leads matched — report not written.")
        return

    if dry_run:
        print(f"  Would write {len(rows)} state(s) to {output_path}")
        return

    fieldnames = [
        "US State",
        "Total number of leads",
        "The lead with most revenue",
        "Total revenue",
        "Median revenue",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Wrote {len(rows)} state(s) to {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import leads from CSV into Close CRM."
    )
    parser.add_argument("--api-key", required=True, help="Your Close API key")
    parser.add_argument("--csv", required=True, help="Path to the input CSV file")
    parser.add_argument(
        "--start", required=False, help="Founded-date range start: DD.MM.YYYY"
    )
    parser.add_argument(
        "--end", required=False, help="Founded-date range end: DD.MM.YYYY"
    )
    parser.add_argument(
        "--output",
        required=False,
        default="state_report.csv",
        help="Output path for the state report CSV (default: state_report.csv)",
    )
    parser.add_argument(
        "--really",
        action="store_true",
        help="Actually import leads and write the report. Without this flag, a dry run is performed.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    dry_run = not args.really

    if dry_run:
        print("Dry run — no data will be written. Add --really to apply changes.\n")

    start_date: datetime | None = None
    end_date: datetime | None = None

    if args.start or args.end:
        if not (args.start and args.end):
            print("Error: --start and --end must both be provided together.")
            sys.exit(1)
        start_date = clean_date(args.start)
        end_date = clean_date(args.end)
        if not start_date or not end_date:
            print("Error: invalid date format — use DD.MM.YYYY.")
            sys.exit(1)
        if start_date > end_date:
            print("Error: --start must be before --end.")
            sys.exit(1)
        print(f"Founded-date filter: {args.start} → {args.end}")
    else:
        print("No date filter applied — all leads will be included in the report.")

    # Step 1: custom fields
    print("\n[1/4] Checking custom fields...")
    custom_fields: dict = ensure_custom_fields(args.api_key, dry_run=dry_run)

    if not dry_run and len(custom_fields) < 2:
        print(
            "Error: could not create required custom fields. Check your API key and permissions."
        )
        sys.exit(1)
    print("  Custom fields ready.")

    # Step 2: parse CSV
    print("\n[2/4] Parsing CSV...")
    try:
        leads, skipped, invalid_values = parse_csv(args.csv, custom_fields)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if skipped:
        print(f"\n  {len(skipped)} row(s) skipped:\n")
        for s in skipped:
            print(f"  • {s['row']} — {s['reason']}")

    if invalid_values:
        print(f"\n  {len(invalid_values)} value(s) were invalid and discarded:\n")
        for v in invalid_values:
            print(f"  • {v['who']}: '{v['value']}' — {v['reason']}")

    # Step 3: upsert leads into Close (or preview in dry-run)
    print(
        "\n[3/4] Checking leads against Close..."
        if dry_run
        else "\n[3/4] Importing leads into Close..."
    )

    created, updated, unchanged, failures = import_leads(
        leads, args.api_key, dry_run=dry_run
    )

    if failures:
        verb = "checked" if dry_run else "imported"
        print(f"\n  The following {len(failures)} lead(s) could not be {verb}:\n")
        for failure in failures:
            print(f"  • {failure['lead']}: {failure['error']}")

    # Step 4: filter, segment, and write report (or preview in dry-run)
    print("\n[4/4] Generating state report...")
    filtered = filter_by_founded(leads, start_date, end_date)

    print(f"  {len(filtered)} lead(s) matched the date filter.")
    rows = segment_by_state(filtered)

    print(f"  {len(rows)} state(s) found.")
    write_state_report(rows, args.output, dry_run=dry_run)

    if dry_run:
        print("\nDry run complete. No data was imported and no files were written.")
        print("Re-run with --really to apply these changes.")
