# PayPal Profit and Loss

This repository provides a Python command line utility that connects to the
PayPal Reporting API, downloads every transaction for a calendar year (or
year-to-date), and prints a profit and loss statement to standard output. The
statement groups totals by currency, includes a monthly breakdown of the net
position, and writes the data to a JSON file for archival or downstream
processing.

## Prerequisites

- Python 3.10 or newer.
- Access to the PayPal REST Reporting API with a client ID and secret.
- The dependencies listed in [`requirements.txt`](requirements.txt).

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

### Create PayPal credentials

1. Sign in to the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/).
2. Under **My Apps & Credentials**, create a **REST API app** (or select an
   existing one) in the desired environment (sandbox or live).
3. Reveal the **Client ID** and **Secret** for the environment you want to
   report on. Copy both values.

### Configure the `.env` file

Store the credentials in a dotenv file (the script loads `.env` by default):

```
PAYPAL_CLIENT_ID=YOUR_CLIENT_ID
PAYPAL_SECRET=YOUR_SECRET
# Optional: uncomment to make sandbox the default
# PAYPAL_ENV=sandbox
```

You can point the tool at a different configuration file with
`--env-file /path/to/paypal.env`.

## Usage

```bash
python paypal_pnl.py [--year YEAR] [--environment {live,sandbox}] \
    [--env-file PATH] [--page-size N] [--json-output PATH] \
    [--no-json-output] [--log-level LEVEL]
```

- `--year` – calendar year to report on. Defaults to the current year (uses
  year-to-date logic if the year has not finished).
- `--environment` – overrides the PayPal environment without changing
  `PAYPAL_ENV`.
- `--env-file` – alternative path to the dotenv file with PayPal credentials.
- `--page-size` – number of transactions requested per API call (maximum 500).
- `--json-output` – overrides the default output file name (`paypal_pnl_<year>.json`).
  Existing files are overwritten.
- `--no-json-output` – skip writing the JSON archive when only console output is needed.
- `--log-level` – adjusts the verbosity of diagnostic logging (e.g. `INFO`).

Example (writes `paypal_pnl_2023.json` alongside console output):

```bash
python paypal_pnl.py --year 2023
```

Sample output:

```
PayPal Profit and Loss Statement
================================
Period: 2023-01-01 to 2023-12-31
Generated on: 2024-01-04 10:03:44 GMT+1
Total transactions considered: 42

Summary by currency:
Currency              Income            Expense               Fees               Net
------------------------------------------------------------------------------------
USD                 12,450.00           2,310.00             325.00           9,815.00

Monthly net totals:
  USD:
     January 2023:         850.00
    February 2023:       1,140.00
       March 2023:       1,205.00
         ...
```

The script exits with a non-zero status code if authentication fails or the API
request encounters an error, making it suitable for automation. The generated
JSON file mirrors the console data so that downstream systems can ingest the
statement without reparsing text output.

## Logging, resilience, and edge cases

- The tool loads credentials from the specified dotenv file and surfaces a
  clear error when the file is missing or incomplete.
- API authentication and retrieval errors include HTTP status and response
  details to aid troubleshooting (for example, hitting rate limits).
- Transactions missing critical fields (net amount, currency, or dates) are
  skipped, counted, and reported at the end of the statement so that data issues
  are visible without breaking the run.
- HTTP responses that cannot be parsed as JSON raise descriptive errors, making
  upstream API issues easier to diagnose.
- Set `--log-level INFO` (or `DEBUG`) to trace pagination progress and dotenv
  parsing decisions when investigating issues.
