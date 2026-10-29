# Project Report Outline (follow the sample report format given by the department)

Cover page: IEM Kolkata, B.Tech, Python Programming Laboratory, **Project title: Inventory and Reorder Tracker**,
names, roll/enrollment numbers, Section B, Department of ECE, session 2026-2027, submitted to, date.
Footer on each page: "Department of Electronics and Communication Engineering | Page N".

1. **Abstract** (150-200 words): problem, tools (Python, CSV, NumPy, Pandas, Matplotlib, JSON), 51 automated tests, key result (demo: 12 products, 303 units, total value shown by the app), limitation (single user).
2. **Keywords**: Inventory management; reorder alerts; Python; CSV; NumPy; Pandas; Matplotlib; unittest.
3. **Introduction**: why small businesses need stock tracking; what the console app does.
4. **Problem Statement and Objectives**: list the 20 required features as bullet objectives.
5. **Software and System Requirements**: table (Python 3.10+, NumPy, Pandas, Matplotlib, unittest, any OS, ~100 MB free).
6. **System Design and Methodology**: layered architecture (App / Reports / Storage / Inventory / records), data schema (two CSV tables), flowchart of stock-out, alert rule table, validation rules.
7. **Implementation of Major Modules**: table "Module/function -> functionality" (as in the sample), snippets of `stock_out`, `alert_level`, `Storage.save`.
8. **Results and Discussion**: screenshots of menu, inventory table, alerts, category report, monthly report, both charts, JSON export; demo-data figures.
9. **Testing and Validation**: table of test cases (expected vs observed), 51/51 pass, plus manual-test table; state clearly what was only manually checked.
10. **Limitations and Future Scope**: single user, no login, CSV not a database, estimated sales only, no suppliers/barcodes; future: SQLite, GUI/web, supplier orders, auto reorder quantity.
11. **Conclusion**.
12. **References**: Python, NumPy, Pandas, Matplotlib docs (same links as the sample).
13. **Appendix**: CSV formats, run/test commands, sample session (`docs/sample_output.txt`), source listing.

Export the finished report from Word/Docs as **Inventory_Reorder_Tracker_Report.pdf**.

# Upload to GitHub (deadline 20 November 2026)
1. Create a free account at github.com. Click **New repository**.
2. Name: `Inventory-and-Reorder-Tracker` (project name), **Public**, do not add a README (you already have one). Create.
3. In the project folder:
```bash
git init
git add .
git commit -m "Inventory and Reorder Tracker - final submission"
git branch -M main
git remote add origin https://github.com/<your-username>/Inventory-and-Reorder-Tracker.git
git push -u origin main
```
4. Copy your final PDF into the folder (e.g. `report/Inventory_Reorder_Tracker_Report.pdf`), then
   `git add . && git commit -m "Add project report" && git push`.
   (No git? On the repo page use **Add file -> Upload files** and drag in the files and folders.)
5. Open the repo link in a private/incognito window to confirm it is publicly visible.
6. **Only one team member** pastes the link into the Google Sheet before 20 Nov 2026.
