"""Inventory and Reorder Tracker - a console-based inventory application.

Layers (top to bottom):
    App          - menu, prompts and formatted output (the only part that prints)
    Reports      - NumPy / Pandas analysis and Matplotlib charts (pure functions)
    Storage      - safe CSV load/save and JSON export
    Inventory    - business rules (stock-in/out, alerts, valuation)
    Product, Transaction - validated records

Reorder alert thresholds (see ``alert_level``):
    NORMAL : quantity <= reorder level
    URGENT : quantity == 0, or quantity <= 50% of the reorder level
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
NORMAL, URGENT = "NORMAL", "URGENT"
STOCK_IN, STOCK_OUT = "IN", "OUT"
MAX_QTY = 10_000_000
MAX_PRICE = Decimal("10000000")
CENT = Decimal("0.01")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
TXN_ID_RE = re.compile(r"^T\d{6,}$")
PRODUCT_FIELDS = ["product_id", "name", "category", "unit_price", "quantity", "reorder_level"]
TXN_FIELDS = ["transaction_id", "timestamp", "product_id", "type", "quantity", "unit_price", "value"]
THRESHOLD_NOTE = "NORMAL: quantity <= reorder level  |  URGENT: quantity is 0 or <= 50% of reorder level"
ESTIMATE_NOTE = ("Estimate only: stock-out quantity x unit price recorded at the time "
                 "of the transaction (not audited sales revenue)")
LEVEL_COLORS = {None: "#2f9e77", NORMAL: "#e8a33d", URGENT: "#d64545"}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class InventoryError(Exception):
    """Base class for all expected, user-facing errors."""


class ValidationError(InventoryError):
    """Input failed validation."""


class DuplicateProductError(InventoryError):
    """Product ID already exists."""


class ProductNotFoundError(InventoryError):
    """Product ID does not exist."""


class InsufficientStockError(InventoryError):
    """Stock-out request exceeds available quantity."""


class StorageError(InventoryError):
    """A file could not be read or written."""


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def clean_text(value: object, label: str, max_len: int = 60) -> str:
    """Trim and collapse whitespace; reject blank or over-long text."""
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        raise ValidationError(f"{label} cannot be blank.")
    if len(text) > max_len:
        raise ValidationError(f"{label} must be at most {max_len} characters.")
    return text


def clean_id(value: object) -> str:
    """Normalise a product ID to upper case and check its format."""
    text = clean_text(value, "Product ID").upper()
    if not ID_RE.match(text):
        raise ValidationError("Product ID must be 1-20 letters, digits, '-' or '_' (no spaces).")
    return text


def parse_quantity(value: object, label: str = "Quantity", minimum: int = 0) -> int:
    """Parse a whole number >= minimum."""
    text = str(value).strip()
    if not re.fullmatch(r"-?\d+", text):
        raise ValidationError(f"{label} must be a whole number.")
    number = int(text)
    if number < minimum:
        raise ValidationError(f"{label} cannot be negative." if minimum == 0
                              else f"{label} must be at least {minimum}.")
    if number > MAX_QTY:
        raise ValidationError(f"{label} cannot exceed {MAX_QTY:,}.")
    return number


def parse_price(value: object) -> float:
    """Parse a non-negative price and round it to 2 decimals (half-up)."""
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise ValidationError("Unit price must be a number.") from None
    if not number.is_finite():
        raise ValidationError("Unit price must be a number.")
    if number < 0:
        raise ValidationError("Unit price cannot be negative.")
    if number > MAX_PRICE:
        raise ValidationError(f"Unit price cannot exceed {MAX_PRICE:,}.")
    return float(number.quantize(CENT, ROUND_HALF_UP))


def line_value(price: float, quantity: int) -> float:
    """Exact currency value (price x quantity) rounded half-up to 2 decimals."""
    return float((Decimal(str(price)) * quantity).quantize(CENT, ROUND_HALF_UP))


def alert_level(quantity: int, reorder_level: int) -> Optional[str]:
    """Return URGENT, NORMAL or None for a stock position (thresholds in module doc)."""
    if quantity == 0 or quantity * 2 <= reorder_level:
        return URGENT
    if quantity <= reorder_level:
        return NORMAL
    return None


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """One inventory item. Fields are validated and normalised on creation."""
    product_id: str
    name: str
    category: str
    unit_price: float
    quantity: int
    reorder_level: int

    def __post_init__(self) -> None:
        self.product_id = clean_id(self.product_id)
        self.name = clean_text(self.name, "Product name")
        self.category = clean_text(self.category, "Category", 40)
        self.unit_price = parse_price(self.unit_price)
        self.quantity = parse_quantity(self.quantity, "Quantity")
        self.reorder_level = parse_quantity(self.reorder_level, "Reorder level")

    @property
    def value(self) -> float:
        """Current stock value of this product."""
        return line_value(self.unit_price, self.quantity)

    @property
    def alert(self) -> Optional[str]:
        return alert_level(self.quantity, self.reorder_level)


@dataclass
class Transaction:
    """One stock movement (type is IN or OUT)."""
    transaction_id: str
    timestamp: str
    product_id: str
    type: str
    quantity: int
    unit_price: float
    value: float

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Transaction":
        """Build a validated Transaction from a CSV row."""
        tid = str(row["transaction_id"]).strip()
        if not TXN_ID_RE.match(tid):
            raise ValidationError(f"Bad transaction ID '{tid}'.")
        stamp = str(row["timestamp"]).strip()
        try:
            datetime.strptime(stamp, TS_FORMAT)
        except ValueError:
            raise ValidationError(f"Bad timestamp '{stamp}'.") from None
        kind = str(row["type"]).strip().upper()
        if kind not in (STOCK_IN, STOCK_OUT):
            raise ValidationError(f"Bad transaction type '{kind}'.")
        return cls(tid, stamp, clean_id(row["product_id"]), kind,
                   parse_quantity(row["quantity"], "Quantity", 1),
                   parse_price(row["unit_price"]), parse_price(row["value"]))


# --------------------------------------------------------------------------- #
# Inventory (business rules)
# --------------------------------------------------------------------------- #
DEMO_PRODUCTS = [  # id, name, category, price, opening stock, reorder level
    ("P001", "Wireless Mouse", "Electronics", 799.00, 60, 20),
    ("P002", "USB-C Cable", "Electronics", 249.00, 120, 40),
    ("P003", "Mechanical Keyboard", "Electronics", 2499.00, 25, 10),
    ("P004", "A4 Notebook", "Stationery", 60.00, 200, 50),
    ("P005", "Gel Pen (Box of 10)", "Stationery", 120.00, 80, 25),
    ("P006", "Stapler", "Stationery", 150.00, 30, 8),
    ("P007", "Office Chair", "Furniture", 5499.00, 15, 5),
    ("P008", "Desk Lamp", "Furniture", 899.00, 20, 6),
    ("P009", "Water Bottle", "Home", 349.00, 70, 20),
    ("P010", "Laptop Stand", "Electronics", 1299.00, 40, 12),
    ("P011", "Whiteboard Marker", "Stationery", 45.00, 150, 60),
    ("P012", "Extension Board", "Electronics", 599.00, 35, 10),
]
DEMO_SALES = [  # product id, quantity sold, days ago
    ("P001", 15, 62), ("P001", 10, 35), ("P001", 12, 9),
    ("P002", 30, 50), ("P002", 45, 20), ("P002", 20, 3),
    ("P003", 6, 40), ("P003", 14, 15),
    ("P004", 40, 58), ("P004", 60, 30), ("P004", 50, 5),
    ("P005", 20, 45), ("P005", 15, 12),
    ("P006", 10, 33), ("P006", 12, 6),
    ("P007", 4, 40), ("P007", 6, 8),
    ("P008", 8, 28), ("P008", 12, 2),
    ("P009", 20, 55), ("P009", 15, 25),
    ("P010", 10, 18),
    ("P011", 30, 47), ("P011", 40, 22), ("P011", 25, 1),
    ("P012", 5, 38), ("P012", 8, 14),
]


class Inventory:
    """Holds products and the transaction log and keeps them consistent."""

    def __init__(self, products: Optional[list[Product]] = None,
                 transactions: Optional[list[Transaction]] = None,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self._products: dict[str, Product] = {p.product_id: p for p in products or []}
        self._transactions: list[Transaction] = list(transactions or [])
        self._clock = clock
        numbers = [int(t.transaction_id[1:]) for t in self._transactions]
        self._next_txn = max(numbers, default=0) + 1

    # ---- read access ------------------------------------------------------ #
    @property
    def products(self) -> list[Product]:
        """Products sorted by ID."""
        return sorted(self._products.values(), key=lambda p: p.product_id)

    @property
    def transactions(self) -> list[Transaction]:
        return list(self._transactions)

    def has_product(self, product_id: str) -> bool:
        try:
            return clean_id(product_id) in self._products
        except ValidationError:
            return False

    def get_product(self, product_id: str) -> Product:
        pid = clean_id(product_id)
        if pid not in self._products:
            raise ProductNotFoundError(f"No product with ID '{pid}'.")
        return self._products[pid]

    def search(self, term: str) -> list[Product]:
        """Case-insensitive substring search over ID, name and category."""
        needle = clean_text(term, "Search term").casefold()
        return [p for p in self.products
                if needle in p.product_id.casefold() or needle in p.name.casefold()
                or needle in p.category.casefold()]

    def alerts(self) -> list[tuple[Product, str]]:
        """Products needing reorder, most urgent first."""
        found = [(p, p.alert) for p in self.products if p.alert]
        found.sort(key=lambda item: (item[1] != URGENT,
                                     item[0].quantity / max(item[0].reorder_level, 1),
                                     item[0].product_id))
        return found

    def total_value(self) -> float:
        return float(sum((Decimal(str(p.value)) for p in self._products.values()), Decimal(0)))

    # ---- write access ----------------------------------------------------- #
    def _canonical_category(self, category: str) -> str:
        """Reuse an existing category spelling so 'toys' and 'Toys' stay together."""
        for p in self._products.values():
            if p.category.casefold() == category.casefold():
                return p.category
        return category

    def _record(self, product: Product, kind: str, quantity: int,
                when: Optional[datetime] = None) -> Transaction:
        txn = Transaction(f"T{self._next_txn:06d}", (when or self._clock()).strftime(TS_FORMAT),
                          product.product_id, kind, quantity, product.unit_price,
                          line_value(product.unit_price, quantity))
        self._next_txn += 1
        self._transactions.append(txn)
        return txn

    def add_product(self, product_id: object, name: object, category: object,
                    unit_price: object, quantity: object, reorder_level: object,
                    when: Optional[datetime] = None) -> Product:
        """Add a product; opening stock (if any) is logged as a stock-in."""
        product = Product(product_id, name, category, unit_price, quantity, reorder_level)
        if product.product_id in self._products:
            raise DuplicateProductError(f"Product ID '{product.product_id}' already exists.")
        product.category = self._canonical_category(product.category)
        self._products[product.product_id] = product
        if product.quantity > 0:
            self._record(product, STOCK_IN, product.quantity, when)
        return product

    def update_product(self, product_id: str, name: object = None, category: object = None,
                       unit_price: object = None, reorder_level: object = None) -> Product:
        """Edit descriptive fields. Quantity is deliberately not changeable here."""
        product = self.get_product(product_id)
        trial = Product(product.product_id,
                        product.name if name is None else name,
                        product.category if category is None else category,
                        product.unit_price if unit_price is None else unit_price,
                        product.quantity,
                        product.reorder_level if reorder_level is None else reorder_level)
        product.name, product.unit_price = trial.name, trial.unit_price
        product.category = self._canonical_category(trial.category)
        product.reorder_level = trial.reorder_level
        return product

    def stock_in(self, product_id: str, quantity: object) -> Transaction:
        product = self.get_product(product_id)
        qty = parse_quantity(quantity, "Quantity", 1)
        if product.quantity + qty > MAX_QTY:
            raise ValidationError(f"Stock would exceed the maximum of {MAX_QTY:,} units.")
        product.quantity += qty
        return self._record(product, STOCK_IN, qty)

    def stock_out(self, product_id: str, quantity: object) -> Transaction:
        product = self.get_product(product_id)
        qty = parse_quantity(quantity, "Quantity", 1)
        if qty > product.quantity:
            raise InsufficientStockError(
                f"Only {product.quantity} unit(s) of {product.product_id} available; "
                f"cannot remove {qty}.")
        product.quantity -= qty
        return self._record(product, STOCK_OUT, qty)

    def seed_demo(self) -> int:
        """Add demo products and history. Existing IDs are never touched."""
        now = self._clock()
        added: set[str] = set()
        opening = now - timedelta(days=90)
        for pid, name, cat, price, qty, reorder in DEMO_PRODUCTS:
            if not self.has_product(pid):
                self.add_product(pid, name, cat, price, qty, reorder, when=opening)
                added.add(pid)
        for pid, qty, days in sorted(DEMO_SALES, key=lambda s: -s[2]):
            product = self._products.get(pid)
            if pid in added and product and qty <= product.quantity:
                product.quantity -= qty
                self._record(product, STOCK_OUT, qty,
                             (now - timedelta(days=days)).replace(hour=11, minute=0, second=0,
                                                                   microsecond=0))
        return len(added)


# --------------------------------------------------------------------------- #
# Storage (CSV persistence, JSON export)
# --------------------------------------------------------------------------- #
class Storage:
    """Reads and writes CSV files safely (temp file + atomic replace + .bak backup)."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.products_path = self.data_dir / "products.csv"
        self.transactions_path = self.data_dir / "transactions.csv"

    # ---- loading ---------------------------------------------------------- #
    @staticmethod
    def _read_rows(path: Path, required: list[str], warnings_out: list[str]) -> list[tuple[int, dict]]:
        if not path.exists():
            return []
        rows: list[tuple[int, dict]] = []
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    return []
                missing = [c for c in required if c not in reader.fieldnames]
                if missing:
                    warnings_out.append(f"{path.name}: missing column(s) {', '.join(missing)}; file ignored.")
                    return []
                for line_no, row in enumerate(reader, start=2):
                    if None in row or any(v is None for v in row.values()):
                        warnings_out.append(f"{path.name} line {line_no}: wrong number of columns; skipped.")
                    else:
                        rows.append((line_no, row))
        except PermissionError:
            raise StorageError(f"Permission denied reading {path}.") from None
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise StorageError(f"Could not read {path.name}: {exc}") from exc
        return rows

    def load(self, clock: Callable[[], datetime] = datetime.now) -> tuple[Inventory, list[str]]:
        """Load data. Returns (inventory, warnings); bad rows are skipped, never fatal."""
        notes: list[str] = []
        products: dict[str, Product] = {}
        for line_no, row in self._read_rows(self.products_path, PRODUCT_FIELDS, notes):
            try:
                product = Product(*(row[f] for f in PRODUCT_FIELDS))
            except InventoryError as exc:
                notes.append(f"{self.products_path.name} line {line_no}: {exc} Row skipped.")
                continue
            if product.product_id in products:
                notes.append(f"{self.products_path.name} line {line_no}: duplicate ID "
                             f"{product.product_id}; row skipped.")
            else:
                products[product.product_id] = product
        txns: dict[str, Transaction] = {}
        for line_no, row in self._read_rows(self.transactions_path, TXN_FIELDS, notes):
            try:
                txn = Transaction.from_row(row)
            except InventoryError as exc:
                notes.append(f"{self.transactions_path.name} line {line_no}: {exc} Row skipped.")
                continue
            if txn.transaction_id in txns:
                notes.append(f"{self.transactions_path.name} line {line_no}: duplicate transaction "
                             f"ID {txn.transaction_id}; row skipped.")
            else:
                txns[txn.transaction_id] = txn
        if notes:
            self._preserve_originals()
        return Inventory(list(products.values()), list(txns.values()), clock), notes

    def _preserve_originals(self) -> None:
        """Copy files that had problems aside so a later save cannot destroy them."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for path in (self.products_path, self.transactions_path):
            try:
                if path.exists():
                    shutil.copy2(path, path.with_name(f"{path.name}.corrupt_{stamp}"))
            except OSError:
                pass

    # ---- saving ----------------------------------------------------------- #
    @staticmethod
    def _write_temp(path: Path, fields: list[str], rows: list[dict]) -> Path:
        temp = path.with_name(path.name + ".tmp")
        with temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return temp

    def save(self, inventory: Inventory) -> None:
        """Write both CSV files. Originals are replaced only after both temp files succeed."""
        jobs = [(self.products_path, PRODUCT_FIELDS, [asdict(p) for p in inventory.products]),
                (self.transactions_path, TXN_FIELDS, [asdict(t) for t in inventory.transactions])]
        temps: list[tuple[Path, Path]] = []
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            for path, fields, rows in jobs:
                temps.append((self._write_temp(path, fields, rows), path))
            for temp, path in temps:
                if path.exists():
                    shutil.copy2(path, path.with_name(path.name + ".bak"))
                os.replace(temp, path)
        except PermissionError:
            raise StorageError(f"Permission denied writing to {self.data_dir}. Data kept in memory.") from None
        except OSError as exc:
            raise StorageError(f"Could not save data: {exc}") from exc
        finally:
            for temp, _ in temps:
                temp.unlink(missing_ok=True)

    # ---- JSON export ------------------------------------------------------ #
    def export_json(self, inventory: Inventory, path: Optional[Path | str] = None) -> Path:
        """Export inventory and transactions to a valid JSON file; returns its path."""
        target = Path(path) if path else (
            self.data_dir / "exports" / f"inventory_export_{datetime.now():%Y%m%d_%H%M%S}.json")
        payload = {
            "exported_at": datetime.now().strftime(TS_FORMAT),
            "inventory": [{**asdict(p), "stock_value": p.value} for p in inventory.products],
            "transactions": [asdict(t) for t in inventory.transactions],
        }
        temp = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(temp, target)
        except PermissionError:
            raise StorageError(f"Permission denied writing {target}.") from None
        except OSError as exc:
            raise StorageError(f"Could not export JSON: {exc}") from exc
        finally:
            temp.unlink(missing_ok=True)
        return target


# --------------------------------------------------------------------------- #
# Reports (NumPy / Pandas) and charts (Matplotlib)
# --------------------------------------------------------------------------- #
CATEGORY_COLUMNS = ["category", "products", "units", "stock_value", "low_stock_items"]
MONTHLY_COLUMNS = ["month", "units_in", "units_out", "value_in", "value_out"]


def products_frame(inv: Inventory) -> pd.DataFrame:
    """Products as a DataFrame with an extra stock_value column."""
    rows = [{**asdict(p), "stock_value": p.value} for p in inv.products]
    return pd.DataFrame(rows, columns=PRODUCT_FIELDS + ["stock_value"])


def transactions_frame(inv: Inventory) -> pd.DataFrame:
    """Transactions as a DataFrame with a real datetime 'timestamp' column."""
    frame = pd.DataFrame([asdict(t) for t in inv.transactions], columns=TXN_FIELDS)
    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], format=TS_FORMAT)
    return frame


def numpy_summary(inv: Inventory) -> Optional[dict[str, float]]:
    """NumPy statistics over per-product quantities; None when inventory is empty."""
    products = inv.products
    if not products:
        return None
    qty = np.array([p.quantity for p in products], dtype=np.int64)
    values = np.array([p.value for p in products], dtype=float)
    return {"products": len(products), "total_units": int(qty.sum()),
            "mean_units": round(float(qty.mean()), 2), "median_units": float(np.median(qty)),
            "min_units": int(qty.min()), "max_units": int(qty.max()),
            "total_value": round(float(values.sum()), 2)}


def category_report(inv: Inventory) -> pd.DataFrame:
    """Pandas group-by: product count, units, stock value and low-stock items per category."""
    frame = products_frame(inv)
    if frame.empty:
        return pd.DataFrame(columns=CATEGORY_COLUMNS)
    frame["low"] = [alert_level(q, r) is not None
                    for q, r in zip(frame["quantity"], frame["reorder_level"])]
    report = frame.groupby("category", as_index=False).agg(
        products=("product_id", "count"), units=("quantity", "sum"),
        stock_value=("stock_value", "sum"), low_stock_items=("low", "sum"))
    report["stock_value"] = report["stock_value"].round(2)
    report["low_stock_items"] = report["low_stock_items"].astype(int)
    return report[CATEGORY_COLUMNS]


def monthly_report(inv: Inventory) -> pd.DataFrame:
    """Stock-in and stock-out units and value per calendar month."""
    frame = transactions_frame(inv)
    if frame.empty:
        return pd.DataFrame(columns=MONTHLY_COLUMNS)
    frame["month"] = frame["timestamp"].dt.strftime("%Y-%m")

    def pivot(column: str) -> pd.DataFrame:
        table = frame.pivot_table(index="month", columns="type", values=column,
                                  aggfunc="sum", fill_value=0)
        return table.reindex(columns=[STOCK_IN, STOCK_OUT], fill_value=0)

    units, values = pivot("quantity"), pivot("value")
    report = pd.DataFrame({"units_in": units[STOCK_IN].astype(int),
                           "units_out": units[STOCK_OUT].astype(int),
                           "value_in": values[STOCK_IN].round(2),
                           "value_out": values[STOCK_OUT].round(2)})
    return report.reset_index()[MONTHLY_COLUMNS]


def daily_stock_out_value(inv: Inventory) -> pd.DataFrame:
    """Estimated stock-out value per date (columns: date, estimated_value)."""
    frame = transactions_frame(inv)
    frame = frame[frame["type"] == STOCK_OUT] if not frame.empty else frame
    if frame.empty:
        return pd.DataFrame(columns=["date", "estimated_value"])
    frame = frame.assign(date=frame["timestamp"].dt.strftime("%Y-%m-%d"))
    daily = frame.groupby("date", as_index=False)["value"].sum()
    daily["estimated_value"] = daily["value"].round(2)
    return daily[["date", "estimated_value"]]


def _start_plot(show: bool):
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _finish_plot(plt, fig, out_path: Path, show: bool) -> Path:
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
    except OSError as exc:
        plt.close(fig)
        raise StorageError(f"Could not save chart: {exc}") from exc
    if show:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            plt.show()
    plt.close(fig)
    return out_path


def stock_chart(inv: Inventory, out_path: Path | str, show: bool = False) -> Optional[Path]:
    """Horizontal bar chart of quantity per product, coloured by alert level."""
    products = inv.products
    if not products:
        return None
    from matplotlib.patches import Patch
    plt = _start_plot(show)
    labels = [f"{p.product_id} {p.name}"[:30] for p in products]
    qty = [p.quantity for p in products]
    fig, ax = plt.subplots(figsize=(10, max(3.5, 0.5 * len(products) + 1.8)))
    pos = np.arange(len(products))
    ax.barh(pos, qty, color=[LEVEL_COLORS[p.alert] for p in products], height=0.62)
    ax.scatter([p.reorder_level for p in products], pos, marker="|", s=320, color="#222222", zorder=3)
    for y, q in zip(pos, qty):
        ax.text(q, y, f"  {q}", va="center", fontsize=9)
    ax.set_yticks(pos, labels)
    ax.invert_yaxis()
    ax.set_xlabel("Units in stock  (black tick = reorder level)")
    ax.set_title("Current Stock by Product", loc="left", fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(handles=[Patch(color=LEVEL_COLORS[None], label="OK"),
                       Patch(color=LEVEL_COLORS[NORMAL], label="Normal alert"),
                       Patch(color=LEVEL_COLORS[URGENT], label="Urgent alert")],
              loc="lower right", frameon=False)
    return _finish_plot(plt, fig, Path(out_path), show)


def sales_chart(inv: Inventory, out_path: Path | str, show: bool = False) -> Optional[Path]:
    """Bar chart of estimated stock-out value per date."""
    daily = daily_stock_out_value(inv)
    if daily.empty:
        return None
    plt = _start_plot(show)
    fig, ax = plt.subplots(figsize=(max(7, 0.55 * len(daily) + 3), 5))
    ax.bar(daily["date"], daily["estimated_value"], color="#4c6ef5", width=0.6)
    ax.set_title("Estimated Stock-Out Value by Date", loc="left", fontweight="bold")
    ax.set_ylabel("Estimated value")
    ax.set_xlabel(ESTIMATE_NOTE, fontsize=8, style="italic")
    ax.tick_params(axis="x", rotation=60)
    ax.spines[["top", "right"]].set_visible(False)
    return _finish_plot(plt, fig, Path(out_path), show)


# --------------------------------------------------------------------------- #
# Console presentation helpers
# --------------------------------------------------------------------------- #
ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def unicode_ok() -> bool:
    return (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "") in ("utf8", "utf8sig")


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def pad(text: str, width: int, align: str = "l") -> str:
    gap = " " * max(0, width - visible_len(text))
    return gap + text if align == "r" else text + gap


def render_table(headers: list[str], rows: list[list[str]], aligns: Optional[list[str]] = None) -> str:
    """Draw a bordered table (Unicode box characters, ASCII fallback)."""
    aligns = aligns or ["l"] * len(headers)
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(visible_len(h), *(visible_len(r[i]) for r in cells)) if cells else visible_len(h)
              for i, h in enumerate(headers)]
    if unicode_ok():
        tl, tm, tr, ml, mm, mr, bl, bm, br, h, v = "┌┬┐├┼┤└┴┘─│"
    else:
        tl = tm = tr = ml = mm = mr = bl = bm = br = "+"
        h, v = "-", "|"

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join(h * (w + 2) for w in widths) + right

    def row(values: list[str]) -> str:
        return v + v.join(f" {pad(val, w, a)} " for val, w, a in zip(values, widths, aligns)) + v

    return "\n".join([rule(tl, tm, tr), row(headers), rule(ml, mm, mr),
                      *[row(r) for r in cells], rule(bl, bm, br)])


def money(amount: float) -> str:
    return f"{'₹' if unicode_ok() else 'Rs '}{amount:,.2f}"


class Theme:
    """Tiny ANSI colour helper that turns itself off when colour is unsupported."""
    CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
             "magenta": "35", "cyan": "36"}

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        return f"\033[{';'.join(self.CODES[s] for s in styles)}m{text}\033[0m"


def detect_color() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # enables ANSI escape processing in Windows terminals
    return True


class Cancelled(Exception):
    """Raised when the user types 'cancel' at a prompt."""


# --------------------------------------------------------------------------- #
# Application (menu)
# --------------------------------------------------------------------------- #
class App:
    """Interactive console menu. Input/output functions are injectable for testing."""

    MENU = [("INVENTORY", [("1", "Add product"), ("2", "View inventory"),
                           ("3", "Search products"), ("4", "Update product")]),
            ("STOCK", [("5", "Stock in"), ("6", "Stock out"), ("7", "Reorder alerts")]),
            ("ANALYTICS", [("8", "Inventory valuation"), ("9", "NumPy summary"),
                           ("10", "Category report (Pandas)"), ("11", "Transaction history"),
                           ("12", "Monthly report")]),
            ("CHARTS & DATA", [("13", "Stock chart"), ("14", "Estimated sales-value chart"),
                               ("15", "Export JSON"), ("16", "Load demo data")]),
            ("", [("0", "Save & exit")])]

    def __init__(self, storage: Storage, inventory: Inventory,
                 input_fn: Callable[[str], str] = input,
                 output_fn: Callable[[str], None] = print,
                 color: bool = False, show_charts: bool = True) -> None:
        self.storage, self.inv = storage, inventory
        self.input_fn, self.say = input_fn, output_fn
        self.t = Theme(color)
        self.show_charts = show_charts
        self.actions = {"1": self.add_product, "2": self.view_inventory, "3": self.search,
                        "4": self.update_product, "5": self.stock_in, "6": self.stock_out,
                        "7": self.show_alerts, "8": self.valuation, "9": self.show_numpy,
                        "10": self.show_categories, "11": self.history, "12": self.show_monthly,
                        "13": self.chart_stock, "14": self.chart_sales, "15": self.export_json,
                        "16": self.load_demo}

    # ---- small UI utilities ----------------------------------------------- #
    def ok(self, text: str) -> None:
        self.say(self.t(f"  {'✔' if unicode_ok() else '[OK]'} {text}", "green"))

    def err(self, text: str) -> None:
        self.say(self.t(f"  {'✖' if unicode_ok() else '[!]'} {text}", "red"))

    def info(self, text: str) -> None:
        self.say(self.t(f"  {text}", "dim"))

    def heading(self, text: str) -> None:
        self.say("\n" + self.t(f"  {text}", "bold", "cyan"))

    def table(self, headers: list[str], rows: list[list[str]], aligns: Optional[list[str]] = None) -> None:
        self.say(render_table([self.t(h, "bold") for h in headers], rows, aligns))

    def alert_cell(self, level: Optional[str]) -> str:
        if level == URGENT:
            return self.t(URGENT, "bold", "red")
        if level == NORMAL:
            return self.t(NORMAL, "yellow")
        return self.t("-", "dim")

    def ask(self, label: str, parse: Callable[[str], object], optional: bool = False):
        """Prompt until parse() accepts the input. 'cancel' aborts the action."""
        suffix = " (Enter to keep)" if optional else ""
        while True:
            raw = self.input_fn(f"  {self.t(label, 'cyan')}{suffix}: ").strip()
            if raw.lower() == "cancel":
                raise Cancelled
            if raw == "" and optional:
                return None
            try:
                return parse(raw)
            except InventoryError as exc:
                self.err(str(exc))

    def ask_product(self) -> Product:
        return self.ask("Product ID", self.inv.get_product)

    def save(self) -> None:
        try:
            self.storage.save(self.inv)
        except StorageError as exc:
            self.err(f"{exc}")

    def status_line(self) -> str:
        products = self.inv.products
        units = sum(p.quantity for p in products)
        alerts = self.inv.alerts()
        urgent = sum(1 for _, lvl in alerts if lvl == URGENT)
        return (f"{len(products)} products  |  {units:,} units  |  {money(self.inv.total_value())}"
                f"  |  {len(alerts)} alerts ({urgent} urgent)")

    def banner(self) -> None:
        lines = ["INVENTORY & REORDER TRACKER", "Console edition  -  data: " + str(self.storage.data_dir)]
        width = max(len(s) for s in lines) + 4
        top, bot, side = ("╭" + "─" * width + "╮", "╰" + "─" * width + "╯", "│") if unicode_ok() else (
            "+" + "-" * width + "+", "+" + "-" * width + "+", "|")
        self.say("\n" + self.t(top, "cyan"))
        for i, line in enumerate(lines):
            self.say(self.t(side, "cyan") + self.t(f"  {line}".ljust(width), *(("bold",) if i == 0 else ("dim",)))
                     + self.t(side, "cyan"))
        self.say(self.t(bot, "cyan"))

    def show_menu(self) -> None:
        self.say("")
        self.say(self.t("  " + self.status_line(), "dim"))
        for title, items in self.MENU:
            if title:
                self.say(self.t(f"\n  {title}", "bold", "magenta"))
            for key, text in items:
                self.say(f"   {self.t(key.rjust(2), 'cyan', 'bold')}  {text}")

    # ---- main loop -------------------------------------------------------- #
    def run(self, warnings_list: Optional[list[str]] = None) -> None:
        self.banner()
        for note in warnings_list or []:
            self.say(self.t(f"  ! {note}", "yellow"))
        try:
            while True:
                self.show_menu()
                choice = self.input_fn(f"\n  {self.t('Choose an option', 'bold')} > ").strip()
                if choice == "0":
                    break
                action = self.actions.get(choice)
                if action is None:
                    self.err(f"'{choice}' is not a valid menu option. Enter a number from 0 to 16.")
                    continue
                try:
                    action()
                except Cancelled:
                    self.info("Cancelled - nothing was changed.")
                except InventoryError as exc:
                    self.err(str(exc))
        except (EOFError, KeyboardInterrupt):
            self.say("")
        self.save()
        self.ok("Data saved. Goodbye!")

    # ---- actions ---------------------------------------------------------- #
    def add_product(self) -> None:
        self.info("Type 'cancel' at any prompt to go back.")

        def new_id(text: str) -> str:
            pid = clean_id(text)
            if self.inv.has_product(pid):
                raise DuplicateProductError(f"Product ID '{pid}' already exists.")
            return pid

        pid = self.ask("Product ID (e.g. P013)", new_id)
        name = self.ask("Product name", lambda s: clean_text(s, "Product name"))
        category = self.ask("Category", lambda s: clean_text(s, "Category", 40))
        price = self.ask("Unit price", parse_price)
        qty = self.ask("Current quantity", lambda s: parse_quantity(s, "Quantity"))
        reorder = self.ask("Reorder level", lambda s: parse_quantity(s, "Reorder level"))
        product = self.inv.add_product(pid, name, category, price, qty, reorder)
        self.save()
        self.ok(f"Added {product.product_id} - {product.name} ({product.quantity} units).")

    def product_rows(self, products: list[Product]) -> list[list[str]]:
        return [[p.product_id, p.name, p.category, money(p.unit_price), str(p.quantity),
                 str(p.reorder_level), money(p.value), self.alert_cell(p.alert)] for p in products]

    PRODUCT_HEADERS = ["ID", "Name", "Category", "Price", "Qty", "Reorder", "Value", "Alert"]
    PRODUCT_ALIGNS = ["l", "l", "l", "r", "r", "r", "r", "l"]

    def view_inventory(self) -> None:
        products = self.inv.products
        if not products:
            self.info("Inventory is empty. Add a product (1) or load demo data (16).")
            return
        self.heading("Inventory")
        self.table(self.PRODUCT_HEADERS, self.product_rows(products), self.PRODUCT_ALIGNS)
        self.info(f"Total stock value: {money(self.inv.total_value())}")

    def search(self) -> None:
        term = self.ask("Search by ID, name or category", lambda s: clean_text(s, "Search term"))
        found = self.inv.search(term)
        if not found:
            self.info(f"No products match '{term}'.")
            return
        self.heading(f"{len(found)} match(es) for '{term}'")
        self.table(self.PRODUCT_HEADERS, self.product_rows(found), self.PRODUCT_ALIGNS)

    def update_product(self) -> None:
        product = self.ask_product()
        self.info(f"Current: {product.name} | {product.category} | {money(product.unit_price)} | "
                  f"reorder level {product.reorder_level}. Stock ({product.quantity}) is not changed here.")
        name = self.ask("New name", lambda s: clean_text(s, "Product name"), optional=True)
        category = self.ask("New category", lambda s: clean_text(s, "Category", 40), optional=True)
        price = self.ask("New unit price", parse_price, optional=True)
        reorder = self.ask("New reorder level", lambda s: parse_quantity(s, "Reorder level"), optional=True)
        if all(v is None for v in (name, category, price, reorder)):
            self.info("No changes entered.")
            return
        self.inv.update_product(product.product_id, name, category, price, reorder)
        self.save()
        self.ok(f"Updated {product.product_id}. Stock remains {product.quantity}.")

    def stock_in(self) -> None:
        product = self.ask_product()
        self.info(f"{product.name}: {product.quantity} in stock.")
        qty = self.ask("Quantity to add", lambda s: parse_quantity(s, "Quantity", 1))
        txn = self.inv.stock_in(product.product_id, qty)
        self.save()
        self.ok(f"{txn.transaction_id}: +{qty} {product.name}. New stock: {product.quantity}.")

    def stock_out(self) -> None:
        product = self.ask_product()
        self.info(f"{product.name}: {product.quantity} in stock.")
        while True:
            qty = self.ask("Quantity to remove", lambda s: parse_quantity(s, "Quantity", 1))
            try:
                txn = self.inv.stock_out(product.product_id, qty)
                break
            except InsufficientStockError as exc:
                self.err(str(exc))
        self.save()
        self.ok(f"{txn.transaction_id}: -{qty} {product.name}. New stock: {product.quantity}.")
        if product.alert:
            self.say(self.t(f"  ! {product.alert} reorder alert for {product.product_id}.",
                            "bold", "red" if product.alert == URGENT else "yellow"))

    def show_alerts(self) -> None:
        alerts = self.inv.alerts()
        self.heading("Reorder alerts")
        self.info(THRESHOLD_NOTE)
        if not alerts:
            self.ok("All products are above their reorder levels.")
            return
        rows = [[p.product_id, p.name, str(p.quantity), str(p.reorder_level),
                 str(max(p.reorder_level - p.quantity, 0)), self.alert_cell(lvl)] for p, lvl in alerts]
        self.table(["ID", "Name", "Qty", "Reorder", "Shortfall", "Alert"], rows,
                   ["l", "l", "r", "r", "r", "l"])

    def valuation(self) -> None:
        products = self.inv.products
        if not products:
            self.info("Inventory is empty - nothing to value.")
            return
        self.heading("Inventory valuation")
        rows = [[p.product_id, p.name, str(p.quantity), money(p.unit_price), money(p.value)] for p in products]
        self.table(["ID", "Name", "Qty", "Unit price", "Stock value"], rows, ["l", "l", "r", "r", "r"])
        self.say(self.t(f"  Total inventory value: {money(self.inv.total_value())}", "bold"))

    def show_numpy(self) -> None:
        stats = numpy_summary(self.inv)
        if stats is None:
            self.info("Inventory is empty - no statistics to show.")
            return
        self.heading("NumPy summary (units per product)")
        rows = [["Products", f"{stats['products']}"], ["Total units", f"{stats['total_units']:,}"],
                ["Mean units", f"{stats['mean_units']:,.2f}"], ["Median units", f"{stats['median_units']:,.1f}"],
                ["Minimum units", f"{stats['min_units']:,}"], ["Maximum units", f"{stats['max_units']:,}"],
                ["Total inventory value", money(stats["total_value"])]]
        self.table(["Metric", "Value"], rows, ["l", "r"])

    def show_categories(self) -> None:
        report = category_report(self.inv)
        if report.empty:
            self.info("Inventory is empty - no category report.")
            return
        self.heading("Category report (Pandas)")
        rows = [[r.category, str(r.products), f"{r.units:,}", money(r.stock_value), str(r.low_stock_items)]
                for r in report.itertuples()]
        self.table(["Category", "Products", "Units", "Stock value", "Low-stock items"], rows,
                   ["l", "r", "r", "r", "r"])

    def history(self) -> None:
        txns = self.inv.transactions
        if not txns:
            self.info("No transactions recorded yet.")
            return

        def parse_limit(text: str) -> int:
            return 0 if text.lower() == "all" else parse_quantity(text, "Count", 1)

        limit = self.ask("How many recent transactions? (number or 'all', Enter = 20)", parse_limit, optional=True)
        limit = 20 if limit is None else limit
        shown = sorted(txns, key=lambda t: (t.timestamp, t.transaction_id), reverse=True)
        shown = shown[:limit] if limit else shown
        self.heading(f"Transaction history ({len(shown)} of {len(txns)})")
        rows = [[t.transaction_id, t.timestamp, t.product_id,
                 self.t(t.type, "green" if t.type == STOCK_IN else "yellow"), str(t.quantity),
                 money(t.unit_price), money(t.value)] for t in shown]
        self.table(["Txn ID", "Timestamp", "Product", "Type", "Qty", "Unit price", "Value"], rows,
                   ["l", "l", "l", "l", "r", "r", "r"])

    def show_monthly(self) -> None:
        report = monthly_report(self.inv)
        if report.empty:
            self.info("No transactions recorded yet - no monthly report.")
            return
        self.heading("Monthly report")
        rows = [[r.month, f"{r.units_in:,}", f"{r.units_out:,}", money(r.value_in), money(r.value_out)]
                for r in report.itertuples()]
        self.table(["Month", "Units in", "Units out", "Value in", "Value out"], rows, ["l", "r", "r", "r", "r"])

    def _chart(self, builder: Callable[..., Optional[Path]], filename: str, empty_msg: str) -> None:
        show = False
        if self.show_charts:
            show = self.input_fn("  Also open the chart in a window? [y/N]: ").strip().lower() in ("y", "yes")
        path = builder(self.inv, self.storage.data_dir / "charts" / filename, show)
        if path is None:
            self.info(empty_msg)
        else:
            self.ok(f"Chart saved to {path}")

    def chart_stock(self) -> None:
        self._chart(stock_chart, "stock_by_product.png", "Inventory is empty - no stock chart.")

    def chart_sales(self) -> None:
        self._chart(sales_chart, "estimated_stock_out_value.png",
                    "No stock-out transactions yet - no sales-value chart.")

    def export_json(self) -> None:
        path = self.storage.export_json(self.inv)
        self.ok(f"Exported {len(self.inv.products)} product(s) and {len(self.inv.transactions)} "
                f"transaction(s) to {path}")

    def load_demo(self) -> None:
        added = self.inv.seed_demo()
        if added:
            self.save()
            self.ok(f"Added {added} demo product(s) with sample history. Existing products were not changed.")
        else:
            self.info("All demo product IDs already exist - nothing added.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory and Reorder Tracker (console)")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"),
                        help="folder for CSV files, charts and exports (default: ./data)")
    args = parser.parse_args(argv)
    storage = Storage(args.data_dir)
    try:
        inventory, notes = storage.load()
    except StorageError as exc:
        print(f"Cannot start: {exc}")
        return 1
    App(storage, inventory, color=detect_color()).run(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
