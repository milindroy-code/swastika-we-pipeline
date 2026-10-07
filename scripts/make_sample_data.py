"""Generate a sample lead sheet and WebEngage exports under data/sample/ for trying the system without real data.

    python scripts/make_sample_data.py
    # then point config.yaml at them:  sources.leads.path: data/sample/leads.csv
    #                                  sources.webengage.folder: data/sample/webengage
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Kolkata")
random.seed(7)

FIRST = ["Aarav", "Ishita", "Kabir", "Meera", "Rahul", "Sneha", "Aditya", "Divya", "Karan", "Nisha",
         "Varun", "Ananya", "Siddharth", "Riya", "Manish", "Kavya", "Harsh", "Tanvi", "Yash", "Pallavi"]
LAST = ["Patel", "Gupta", "Reddy", "Nair", "Joshi", "Agarwal", "Shah", "Das", "Kulkarni", "Malhotra"]
CATEGORIES = ["Registered - KYC pending", "KYC done - No deposit", "Funded - No trade", "Dormant trader"]
SYMBOLS = ["RELIANCE", "TATAMOTORS", "HDFCBANK", "INFY", "SBIN", "ITC", "NIFTYBEES", "ADANIPORTS", "IRFC", "ZOMATO"]

JOURNEYS = {
    "Registered - KYC pending": ["App Opened", "KYC Started", "App Opened"],
    "KYC done - No deposit": ["App Opened", "KYC Completed", "Stock Viewed", "Add Funds Clicked", "Deposit Failed"],
    "Funded - No trade": ["App Opened", "Deposit Successful", "Stock Viewed", "Added To Watchlist", "Order Window Closed"],
    "Dormant trader": ["App Opened", "Stock Viewed"],
}


def main() -> None:
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    data = ROOT / "data" / "sample"
    (data / "webengage").mkdir(parents=True, exist_ok=True)

    leads, events, profiles = [], [], []
    for i in range(1, 41):
        name = f"{random.choice(FIRST)} {random.choice(LAST)}"
        phone = f"9{random.randint(100000000, 999999999)}"
        category = CATEGORIES[i % len(CATEGORIES)]
        uid = f"WE{i:05d}"
        row = {"Lead ID": f"L{i:04d}", "WebEngage User ID": uid, "Name": name, "Phone": phone,
               "Email": f"{name.split()[0].lower()}{i}@example.com", "Category": category,
               "Source": random.choice(["Meta Ads", "Google Ads", "Referral", "Organic"]),
               "Remarks": "", "Last Call Status": "", "Last Call Time": "", "Next Follow-up": ""}
        if i % 9 == 0:  # NPC already recorded in the sheet before the system existed
            row["Last Call Status"] = "NPC"
            row["Last Call Time"] = (now - timedelta(hours=random.randint(1, 6))).strftime("%Y-%m-%d %H:%M")
        if i % 11 == 0:  # caller promised a callback
            row["Next Follow-up"] = (now + timedelta(hours=random.randint(1, 5))).strftime("%Y-%m-%d %H:%M")
        leads.append(row)
        profiles.append({"User ID": uid, "First Name": name.split()[0], "Phone": phone, "Email": row["Email"],
                         "City": random.choice(["Indore", "Mumbai", "Pune", "Jaipur", "Delhi"])})

        t = now - timedelta(days=random.randint(1, 10), hours=random.randint(0, 8))
        journey = JOURNEYS[category] + random.sample(["App Opened", "Stock Viewed", "App Opened"], k=random.randint(0, 3))
        if category == "Dormant trader":
            events.append({"User ID": uid, "Event Name": "Order Placed", "Event Time": (now - timedelta(days=40)).isoformat(),
                           "Symbol": random.choice(SYMBOLS), "Amount": ""})
        for ev in journey:
            t += timedelta(hours=random.randint(1, 20))
            if t > now:
                t = now - timedelta(minutes=random.randint(5, 600))
            events.append({"User ID": uid, "Event Name": ev, "Event Time": t.isoformat(),
                           "Symbol": random.choice(SYMBOLS) if ev in {"Stock Viewed", "Added To Watchlist", "Order Window Closed"} else "",
                           "Amount": str(random.choice([2000, 5000, 10000, 25000])) if "Fund" in ev or "Deposit" in ev else ""})

    def write(path: Path, items: list[dict]) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(items[0]))
            w.writeheader()
            w.writerows(items)

    write(data / "leads.csv", leads)
    write(data / "webengage" / "sample_events_export.csv", events)
    write(data / "webengage" / "sample_users_export.csv", profiles)
    notes = ROOT / "data" / "market_notes.md"
    if not notes.exists():
        notes.write_text(
            "# Market notes (SAMPLE: replace every morning with real notes)\n"
            "- [Sample] Benchmark indices opened higher; banking and auto led\n"
            "- [Sample] Quarterly results season is under way: IT majors report this week\n"
            "- [Sample] Two mainboard IPOs open for subscription this week\n",
            encoding="utf-8",
        )
    print(f"Wrote {len(leads)} leads, {len(events)} events, {len(profiles)} profiles to {data}")


if __name__ == "__main__":
    main()
