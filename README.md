# Close CRM CSV importer

A script that reads a CSV file of leads and imports them into [Close CRM](https://close.com). It cleans and validates the data before importing, detects records that already exist in Close, and generates a report that breaks down leads by US state.

---

## What the script does

1. **Reads your CSV** and checks that all required columns are present.
2. **Cleans and validates** every value. It drops anything that looks wrong rather than importing bad data.
3. **Groups rows by company name** so that multiple rows for the same company become one lead with multiple contacts.
4. **Checks Close** for each lead before writing anything. If a lead already exists, the script only fills in fields that are blank, rather than overwriting existing data.
5. **Generates a state report** CSV that summarises your leads by US state.

By default the script runs in **dry-run mode**. It checks everything and tells you what it would do, but writes nothing. Add `--really` when you are ready to apply the changes.

---

## How invalid data is handled

Before anything is imported, every value in the CSV goes through a cleaning step:

- **Phone numbers**: all non-numeric characters (spaces, dashes, brackets, emojis) are stripped. If the number has no `+` at the start, it is treated as a US number and `+1` is prepended, matching what Close does when you type a number directly into its UI. If no digits remain after cleaning, the value is discarded.
- **Email addresses**: checked against a standard format (local part, `@`, domain, extension). Anything that does not look like a valid email is discarded.
- **Dates**": must be in `DD.MM.YYYY` format. Anything else is ignored.
- **Revenue**: currency symbols, commas, and spaces are stripped and the result is parsed as a number. If it cannot be parsed, it is ignored.

Invalid values are discarded silently. The rest of the lead or contact is still imported. At the end of the parsing step the script prints a summary of everything that was dropped, so you can review it before committing.

Entire rows are skipped only when the **Company name is missing**, since that is the field used to identify and group leads.

---

## How leads are found and grouped

The CSV may contain multiple rows for the same company (e.g. one row per contact(. The script groups all rows that share the same company name into a single lead.

If two rows for the same company carry different contact names, both contacts are added to the lead. If the same contact name appears more than once, the script merges them and adds any new emails or phone numbers from the later row without duplicating ones that are already there.

When importing, the script searches Close by company name before creating anything. If a match is found:

- Lead-level fields (state, founded date, revenue) are only written if that field is currently blank in Close. Existing values are never overwritten.
- Contacts are matched by name. New contacts are added, while existing contacts receive any new emails or phone numbers from the CSV that are not already in Close.

If no match is found, the lead is created fresh.

---

## How leads are segmented by state and ranked by revenue

After importing, the script groups all leads by their **US State** field. For each state it calculates:

- **Total number of leads** in that state.
- **The lead with the most revenue**
- **Total revenue**: the sum of all revenue values for leads in that state.
- **Median revenue**: the middle value when all revenues in the state are sorted, which gives a better sense of the typical company than the average (which can be skewed by one very large number).

The result is written to a CSV file, sorted alphabetically by state.

You can optionally filter leads by their **founded date** before segmenting, so only leads founded within the date range you specify will appear in the report.

---

## Requirements

You need:

- **Python 3.10 or later**: Check your version by running `python3 --version`
- **A Close API key**: found in Close under Settings → API Keys

---

## How to run

### Basic usage (dry run)

In your terminal:

```bash
python3 import_leads_from_csv.py --api-key YOUR_API_KEY --csv path/to/leads.csv
```

This checks everything and prints a summary of what would happen, but does **not** write anything to Close or create any files.

### Actually import

```bash
python3 import_leads_from_csv.py --api-key YOUR_API_KEY --csv path/to/leads.csv --really
```

### Filter by founded date

Only include leads founded within a date range in the state report:

```bash
python3 import_leads_from_csv.py --api-key YOUR_API_KEY --csv path/to/leads.csv --really --start 01.01.2010 --end 31.12.2020
```

Dates must be in `DD.MM.YYYY` format. Both `--start` and `--end` must be provided together.

### Change the report output path

By default the state report is written to `state_report.csv` in the current directory:

```bash
python3 import_leads_from_csv.py --api-key YOUR_API_KEY --csv path/to/leads.csv --really --output reports/states.csv
```

---

## Expected CSV format

The CSV must have exactly these column headers:

| Column                   | Description                                                            |
| ------------------------ | ---------------------------------------------------------------------- |
| `Company`                | Company name, required. Rows without this are skipped                  |
| `Contact Name`           | Full name of the contact at this company                               |
| `Contact Emails`         | One or more email addresses, separated by `,` `;` or space             |
| `Contact Phones`         | One or more phone numbers, separated by `,` `;` or space               |
| `custom.Company Founded` | Date the company was founded, in `DD.MM.YYYY` format                   |
| `custom.Company Revenue` | Annual revenue. Currency symbols and commas are stripped automatically |
| `Company US State`       | Two-letter US state code (e.g. `CA`, `NY`)                             |
