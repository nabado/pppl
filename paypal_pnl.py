#!/usr/bin/env python3
"""Generate a PayPal profit and loss statement for a calendar year.

This script connects to the PayPal Reporting API, downloads every transaction
for a calendar year (or year-to-date), and summarises them into a profit and
loss statement. The resulting statement is printed to stdout and, by default,
also persisted as JSON for the reported year. Totals are presented by currency
and include a monthly breakdown of net activity.

Credentials are sourced from a ``.env`` file containing ``PAYPAL_CLIENT_ID``
and ``PAYPAL_SECRET`` entries. The PayPal environment can be selected with the
``--environment`` option or the ``PAYPAL_ENV`` environment variable. Only the
``live`` and ``sandbox`` environments are supported.
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import sys
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import (
    Dict,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

import requests

LOGGER = logging.getLogger(__name__)

PAYPAL_API_BASE_URLS = {
    "live": "https://api-m.paypal.com",
    "sandbox": "https://api-m.sandbox.paypal.com",
}

TWOPLACES = Decimal("0.01")


@dataclass
class CurrencySummary:
    """Aggregated totals for a single currency."""

    currency: str
    income: Decimal = field(default_factory=lambda: Decimal("0"))
    expense: Decimal = field(default_factory=lambda: Decimal("0"))
    fees: Decimal = field(default_factory=lambda: Decimal("0"))
    net: Decimal = field(default_factory=lambda: Decimal("0"))

    def register(self, amount: Decimal, fee: Decimal) -> None:
        """Register a transaction amount and its fee."""

        if amount >= 0:
            self.income += amount
        else:
            self.expense += -amount
        self.net += amount
        if fee:
            self.fees += abs(fee)


@dataclass
class ProfitAndLossStatement:
    """Container for the generated profit and loss statement."""

    start: datetime
    end: datetime
    currency_summaries: Iterable[CurrencySummary]
    monthly_net: Mapping[str, Mapping[Tuple[int, int], Decimal]]
    transaction_count: int
    skipped_transactions: Mapping[str, int] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def render(self) -> str:
        lines = []
        title = "PayPal Profit and Loss Statement"
        lines.append(title)
        lines.append("=" * len(title))
        lines.append(
            f"Period: {self.start.date().isoformat()} to {self.end.date().isoformat()}"
        )
        lines.append(
            "Generated on: "
            + self.generated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        lines.append("Total transactions considered: " + str(self.transaction_count))
        lines.append("")

        summaries = list(self.currency_summaries)
        if not summaries:
            lines.append("No transactions found for the requested period.")
            return "\n".join(lines)

        lines.append("Summary by currency:")
        header = f"{'Currency':<10}{'Income':>18}{'Expense':>18}{'Fees':>18}{'Net':>18}"
        lines.append(header)
        lines.append("-" * len(header))
        for summary in summaries:
            lines.append(
                f"{summary.currency:<10}"
                f"{format_money(summary.income):>18}"
                f"{format_money(summary.expense):>18}"
                f"{format_money(summary.fees):>18}"
                f"{format_money(summary.net):>18}"
            )

        lines.append("")
        lines.append("Monthly net totals:")
        for currency in sorted(self.monthly_net):
            lines.append(f"  {currency}:")
            totals = self.monthly_net[currency]
            if not totals:
                lines.append("    (no transactions)")
                continue
            for (year, month), value in totals.items():
                month_name = calendar.month_name[month]
                lines.append(
                    f"    {month_name:>9} {year}: {format_money(value):>15}"
                )

        if self.skipped_transactions:
            lines.append("")
            lines.append("Transactions skipped due to incomplete data:")
            for reason, count in self.skipped_transactions.items():
                lines.append(f"  {reason}: {count}")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        currencies = []
        for summary in self.currency_summaries:
            currencies.append(
                {
                    "currency": summary.currency,
                    "income": decimal_to_string(summary.income),
                    "expense": decimal_to_string(summary.expense),
                    "fees": decimal_to_string(summary.fees),
                    "net": decimal_to_string(summary.net),
                }
            )

        monthly_breakdown: Dict[str, list] = {}
        for currency, totals in self.monthly_net.items():
            entries = []
            for (year, month), value in totals.items():
                entries.append(
                    {
                        "year": year,
                        "month": month,
                        "month_name": calendar.month_name[month],
                        "net": decimal_to_string(value),
                    }
                )
            monthly_breakdown[currency] = entries

        return {
            "period": {
                "start": self.start.astimezone(timezone.utc).isoformat(),
                "end": self.end.astimezone(timezone.utc).isoformat(),
            },
            "generated_at": self.generated_at.astimezone(timezone.utc).isoformat(),
            "transaction_count": self.transaction_count,
            "currencies": currencies,
            "monthly_net": monthly_breakdown,
            "skipped_transactions": dict(self.skipped_transactions),
        }


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PayPal profit and loss statement for a calendar year."
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Calendar year to report on. Defaults to the current year.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help=(
            "Path to the .env file containing PAYPAL_CLIENT_ID and PAYPAL_SECRET. "
            "Defaults to '.env'."
        ),
    )
    parser.add_argument(
        "--environment",
        choices=sorted(PAYPAL_API_BASE_URLS.keys()),
        help=(
            "PayPal environment to use. Defaults to the value of PAYPAL_ENV "
            "or 'live' if not set."
        ),
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=500,
        help="Number of transactions to request per API call (max 500).",
    )
    parser.add_argument(
        "--json-output",
        help=(
            "Optional path for the JSON statement. Defaults to 'paypal_pnl_<year>.json' "
            "in the current directory."
        ),
    )
    parser.add_argument(
        "--no-json-output",
        action="store_true",
        help="Skip writing the JSON output file.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Logging level (e.g. INFO, DEBUG). Defaults to WARNING.",
    )
    return parser.parse_args(argv)


def determine_environment(explicit: Optional[str]) -> str:
    env = (explicit or os.environ.get("PAYPAL_ENV", "live")).strip().lower()
    if env not in PAYPAL_API_BASE_URLS:
        raise ValueError(
            f"Unsupported PayPal environment '{env}'. Expected one of: "
            + ", ".join(sorted(PAYPAL_API_BASE_URLS))
        )
    return env


def determine_period(year: Optional[int]) -> Tuple[datetime, datetime]:
    today = datetime.now(timezone.utc)
    current_year = today.year
    if year is None:
        year = current_year
    if year > current_year:
        raise ValueError("The requested year is in the future.")

    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    if year == current_year:
        end = today
    else:
        end = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


def to_paypal_isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_env_file(path: Path, required_keys: Sequence[str]) -> None:
    expanded = path.expanduser()
    if not expanded.is_file():
        raise RuntimeError(
            f"Environment file '{expanded}' was not found. Create it with the required "
            "PayPal credentials (PAYPAL_CLIENT_ID and PAYPAL_SECRET)."
        )

    LOGGER.debug("Loading environment variables from %s", expanded)
    try:
        with expanded.open("r", encoding="utf-8") as handle:
            for lineno, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    LOGGER.warning(
                        "Ignoring malformed line %d in %s", lineno, expanded
                    )
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key:
                    LOGGER.warning(
                        "Encountered empty key on line %d in %s", lineno, expanded
                    )
                    continue
                os.environ.setdefault(key, value)
    except OSError as exc:
        raise RuntimeError(f"Unable to read environment file '{expanded}': {exc}") from exc

    missing = [key for key in required_keys if not os.environ.get(key)]
    if missing:
        raise RuntimeError(
            "Missing required credentials after loading the environment file: "
            + ", ".join(missing)
        )


def load_credentials() -> Tuple[str, str]:
    try:
        client_id = os.environ["PAYPAL_CLIENT_ID"]
        secret = os.environ["PAYPAL_SECRET"]
    except KeyError as exc:  # pragma: no cover - defensive programming
        missing = exc.args[0]
        raise RuntimeError(
            f"Missing required environment variable: {missing}"  # pragma: no cover
        ) from exc
    if not client_id or not secret:
        raise RuntimeError("PayPal credentials must not be empty.")
    return client_id, secret


def get_access_token(base_url: str, client_id: str, secret: str) -> str:
    LOGGER.debug("Requesting OAuth token from %s", base_url)
    response = requests.post(
        f"{base_url}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, secret),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Failed to obtain an access token from PayPal.")
    return token


def safe_int(value: object) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_transactions(
    base_url: str,
    access_token: str,
    start: datetime,
    end: datetime,
    page_size: int,
) -> Iterator[Mapping[str, object]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "start_date": to_paypal_isoformat(start),
        "end_date": to_paypal_isoformat(end),
        "fields": "all",
        "page_size": max(1, min(page_size, 500)),
        "page": 1,
        "transaction_status": "S",  # settled transactions only
    }
    url = f"{base_url}/v1/reporting/transactions"

    while True:
        page_for_log = "?"
        if isinstance(params, dict):
            page_for_log = params.get("page", "?")
        LOGGER.debug("Fetching transactions page %s", page_for_log)
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Failed to decode PayPal transaction response as JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("Unexpected transaction response format from PayPal.")
        for detail in payload.get("transaction_details", []) or []:
            yield detail

        total_pages = safe_int(payload.get("total_pages"))
        current_page = safe_int(payload.get("page"))
        if (
            isinstance(params, dict)
            and total_pages
            and current_page
            and current_page < total_pages
        ):
            params["page"] = current_page + 1
            continue

        links = payload.get("links", []) or []
        next_link = next(
            (link for link in links if isinstance(link, Mapping) and link.get("rel") == "next"),
            None,
        )
        if next_link and next_link.get("href"):
            url = next_link["href"]
            params = None  # the link already encodes all necessary parameters
            continue
        break


def decimal_from_paypal(value: object) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        LOGGER.debug("Unable to parse decimal value '%s'", value)
        return Decimal("0")


def parse_paypal_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+0000"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    LOGGER.debug("Unable to parse PayPal datetime '%s'", value)
    return None


def compute_statement(
    transactions: Iterable[Mapping[str, object]],
    start: datetime,
    end: datetime,
) -> ProfitAndLossStatement:
    currency_totals: Dict[str, CurrencySummary] = {}
    monthly_net: Dict[str, MutableMapping[Tuple[int, int], Decimal]] = {}
    transaction_count = 0
    skipped_reasons: MutableMapping[str, int] = defaultdict(int)

    for detail in transactions:
        info = detail.get("transaction_info", {}) if isinstance(detail, Mapping) else {}
        net_data = info.get("net_amount") if isinstance(info, Mapping) else None
        if not isinstance(net_data, Mapping):
            skipped_reasons["missing_net_amount"] += 1
            continue
        currency = net_data.get("currency_code")
        if not currency:
            skipped_reasons["missing_currency_code"] += 1
            continue

        amount = decimal_from_paypal(net_data.get("value"))
        fee_data = info.get("fee_amount") if isinstance(info, Mapping) else None
        fee_amount = decimal_from_paypal(
            fee_data.get("value") if isinstance(fee_data, Mapping) else None
        )

        summary = currency_totals.setdefault(currency, CurrencySummary(currency))
        summary.register(amount, fee_amount)
        transaction_count += 1

        date_value = (
            info.get("transaction_effective_date")
            if isinstance(info, Mapping)
            else None
        )
        if not date_value and isinstance(info, Mapping):
            date_value = info.get("transaction_initiation_date")
        dt = parse_paypal_datetime(date_value if isinstance(date_value, str) else None)
        if dt is None:
            skipped_reasons["unparseable_transaction_date"] += 1
            continue
        dt = dt.astimezone(timezone.utc)
        currency_months = monthly_net.setdefault(currency, OrderedDict())
        key = (dt.year, dt.month)
        currency_months[key] = currency_months.get(key, Decimal("0")) + amount

    ordered_monthly: Dict[str, Mapping[Tuple[int, int], Decimal]] = {}
    for currency, totals in monthly_net.items():
        ordered = OrderedDict()
        for key in sorted(totals):
            ordered[key] = totals[key]
        ordered_monthly[currency] = ordered

    summaries = [currency_totals[c] for c in sorted(currency_totals)]
    return ProfitAndLossStatement(
        start=start,
        end=end,
        currency_summaries=summaries,
        monthly_net=ordered_monthly,
        transaction_count=transaction_count,
        skipped_transactions=OrderedDict(sorted(skipped_reasons.items())),
    )


def format_money(value: Decimal) -> str:
    quantized = value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    return f"{quantized:,.2f}"


def decimal_to_string(value: Decimal) -> str:
    quantized = value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def format_http_error(exc: requests.HTTPError) -> str:
    response = exc.response
    status = response.status_code if response is not None else "unknown"
    message = response.reason if response is not None else str(exc)
    if response is not None:
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, Mapping):
            message = (
                data.get("message")
                or data.get("error_description")
                or data.get("details")
                or message
            )
        elif response.text:
            message = response.text[:500]
    return f"HTTP {status}: {message}"


def determine_output_path(explicit_path: Optional[str], start: datetime) -> Path:
    if explicit_path:
        return Path(explicit_path).expanduser()
    filename = f"paypal_pnl_{start.year}.json"
    return Path.cwd() / filename


def write_statement_json(
    statement: ProfitAndLossStatement,
    path: Path,
    environment: str,
) -> None:
    payload = statement.to_dict()
    payload["environment"] = environment
    LOGGER.info("Writing JSON statement to %s", path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise RuntimeError(f"Failed to write JSON output to '{path}': {exc}") from exc


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    env_path = Path(args.env_file)
    try:
        load_env_file(env_path, ("PAYPAL_CLIENT_ID", "PAYPAL_SECRET"))
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        environment = determine_environment(args.environment)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        start, end = determine_period(args.year)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        client_id, secret = load_credentials()
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        token = get_access_token(PAYPAL_API_BASE_URLS[environment], client_id, secret)
    except requests.HTTPError as exc:
        LOGGER.error(
            "Failed to authenticate with PayPal: %s", format_http_error(exc)
        )
        return 1
    except requests.RequestException as exc:
        LOGGER.error("HTTP error while authenticating with PayPal: %s", exc)
        return 1
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        transactions = fetch_transactions(
            PAYPAL_API_BASE_URLS[environment],
            token,
            start,
            end,
            args.page_size,
        )
        statement = compute_statement(transactions, start, end)
    except requests.HTTPError as exc:
        LOGGER.error(
            "Failed to retrieve transactions: %s", format_http_error(exc)
        )
        return 1
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 1
    except requests.RequestException as exc:
        LOGGER.error("HTTP error while retrieving transactions: %s", exc)
        return 1

    print(statement.render())

    if not args.no_json_output:
        output_path = determine_output_path(args.json_output, start)
        try:
            write_statement_json(statement, output_path, environment)
        except RuntimeError as exc:
            LOGGER.error("%s", exc)
            return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
