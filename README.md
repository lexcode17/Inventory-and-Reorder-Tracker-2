# Inventory and Reorder Tracker (Console Application)

A menu-driven Python 3.10+ console app that tracks products, stock movements and reorder alerts.
Uses **CSV** storage, **NumPy** statistics, **Pandas** reports, **Matplotlib** charts, **JSON** export
and **unittest** tests.

## Folder structure
```
Inventory-and-Reorder-Tracker/
├── inventory_tracker.py          # whole application (classes + functions)
├── requirements.txt              # numpy, pandas, matplotlib
├── README.md
├── .gitignore
├── tests/test_inventory_tracker.py   # 51 unittest tests (temp directories only)
├── docs/
│   ├── sample_output.txt         # real output of a scripted session
│   └── PROJECT_REPORT_OUTLINE.md # report outline + GitHub upload steps
└── data/                         # created at runtime: products.csv, transactions.csv, charts/, exports/
```

## Install and run
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python inventory_tracker.py                 # data stored in ./data
python inventory_tracker.py --data-dir D:\my_data   # optional custom folder
```
Run tests: `python -m unittest discover -s tests -v`

## Architecture (explain this in the viva)
| Piece | Role |
|---|---|
| `Product`, `Transaction` (dataclasses) | A record. `Product.__post_init__` validates/normalises every field, so an invalid product can never exist. |
| `Inventory` | Business rules: `add_product`, `update_product`, `stock_in`, `stock_out`, `search`, `alerts`, `total_value`, `seed_demo`. Validates **before** changing anything, so stock and the log never disagree. |
| `Storage` | `load()` skips bad CSV rows with warnings (and copies the originals to `*.corrupt_<time>`); `save()` writes temp files, keeps a `.bak`, then atomically replaces; `export_json()`. |
| `numpy_summary`, `category_report`, `monthly_report`, `daily_stock_out_value` | Analysis (NumPy / Pandas). Return empty results for empty data. |
| `stock_chart`, `sales_chart` | Matplotlib charts saved as PNG (optionally shown in a window). Return `None` when there is nothing to plot. |
| `App` | Menu, prompts, tables. Input/output functions are injectable, so the menu itself is tested. |
| Validators | `clean_text`, `clean_id`, `parse_quantity`, `parse_price`, `line_value` (Decimal, half-up rounding to 2 dp). |

### How data is stored
`products.csv`: `product_id,name,category,unit_price,quantity,reorder_level`
`transactions.csv`: `transaction_id,timestamp,product_id,type,quantity,unit_price,value`
(IDs `T000001…`, type `IN`/`OUT`, timestamp `YYYY-MM-DD HH:MM:SS`). Data is saved after every change and on exit.

### Stock-in / stock-out
Both parse the quantity (whole number >= 1), look up the product, change `quantity`, and append a
transaction using the product's current price (`value = price x quantity`). Stock-out **rejects** a
quantity above the available stock and changes nothing. Opening stock of a new product is logged as a stock-in.

### Reorder alerts (documented thresholds)
* **NORMAL**: `quantity <= reorder level`
* **URGENT**: `quantity == 0` or `quantity <= 50% of reorder level` (integer test `quantity * 2 <= reorder`)

Example, reorder level 10: 11 -> none, 10 -> NORMAL, 6 -> NORMAL, 5 -> URGENT, 0 -> URGENT.

### Sales-value chart
Uses stock-out transactions grouped by date and is labelled **"Estimated"**: it is quantity x the price
recorded at the time, not audited revenue (discounts/returns are not tracked).

## Manual test of every menu option (start with an empty `--data-dir`)
| # | Option | Input | Expected |
|---|---|---|---|
| 1 | Add product | `P100`, `Test Pen`, `Stationery`, `12.50`, `40`, `10` | "Added P100…". Retry with `P100` again -> "already exists"; price `-5` -> "cannot be negative"; qty `abc` -> "must be a whole number" |
| 2 | View inventory | – | Table with P100, value ₹500.00 |
| 3 | Search | `pen` / `stat` / `p1` | P100 listed each time; `zzz` -> "No products match" |
| 4 | Update | `P100`, name `Gel Pen`, Enter x3 | Name changes, stock still 40 |
| 5 | Stock in | `P100`, `10` | `T000002: +10…`, stock 50 |
| 6 | Stock out | `P100`, `100` -> rejected; then `45` | Stock 5, "URGENT"/"NORMAL" alert shown (reorder 10 -> 5 is URGENT) |
| 7 | Reorder alerts | – | P100 listed as URGENT |
| 8 | Valuation | – | Per-product values and total |
| 9 | NumPy summary | – | Total units, mean, median, min, max, total value |
| 10 | Category report | – | Products, units, value, low-stock per category |
| 11 | History | Enter | All transactions with timestamps |
| 12 | Monthly report | – | Current month: units in/out and values |
| 13 / 14 | Charts | `n` | PNG saved in `data/charts/` (answer `y` to open a window) |
| 15 | Export JSON | – | File in `data/exports/`; open it to check it is valid JSON |
| 16 | Demo data | – | 12 products added; run again -> "nothing added"; existing IDs never overwritten |
| 0 | Exit | – | "Data saved. Goodbye!"; restart the app and confirm data persists |
Other checks: invalid menu choice (`abc`, `99`), unknown ID, `cancel` at any prompt, delete `data/` (app recreates it),
corrupt a CSV row (app warns and skips it).

## Testing status
* **Executed automatically:** `python -m unittest discover -s tests` -> 51 tests, all passing at packaging time
  (Python 3.12, NumPy 2.4, Pandas 3.0, Matplotlib 3.10). Covers validation, duplicates, stock-in/out,
  boundaries, valuation, CSV save/reload, corrupt data, permission errors (simulated), JSON, empty reports, the menu with scripted input.
* **Needs manual verification on your machine:** colours and box characters in your terminal, the
  interactive chart window (`y` at the chart prompt), real file-permission situations, Windows/macOS behaviour.
* Passing tests are evidence, not proof that the program is bug-free.
