"""Synthetic but realistic sample data for local runs and tests.

Produces, under <root>/:
  inbound/UK/TESCO/tesco_weekly_epos_2026w40.xlsx   title rows above header, Sales + Stores sheets, total row
  inbound/UK/TESCO/tesco_toolkit_daily_2026-10-11.csv  ';'-delimited daily store file, same layout as
                                                    Veritas mapping #4285 (known template)
  inbound/IN/DMART/dmart_sellout_2026-10-04.csv     outlet attributes embedded, EANs mangled by Excel
  veritas/veritas_4285_tesco_toolkit_daily.json     the real Veritas mapping shared by Reckitt
  masters/erp_product_master.csv                    ERP product master (RB codes, EAN, global attributes)
  masters/rb_customer_master.csv                    RB customer master (outlet codes, address, channel)
  existing_bronze/bronze.pos_sellout.csv            what Veritas loaded from the daily file (for reconciliation)
  ground_truth/product_xref.csv, outlet_xref.csv    correct answers, used by tests to score accuracy

Everything is deterministic (fixed seed). Retailer descriptions are deliberately abbreviated
and some EANs are missing, truncated or in scientific notation, so harmonisation has to fall
back to description + attribute matching on a realistic share of products.
"""
from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import pandas as pd


def ean13(base12: str) -> str:
    digits = [int(d) for d in base12]
    total = sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits))
    return base12 + str((10 - total % 10) % 10)


# (rb_code, brand, category, sub_category, erp_description, size, unit, pack_count,
#  tesco_desc, dmart_desc)
PRODUCTS = [
    ("RB100101", "Dettol", "Health & Hygiene", "Hand Wash", "Dettol Original Antibacterial Liquid Hand Wash 250ml", 250, "ml", 1,
     "DETTOL ANTIBAC HW ORIG 250ML", "Dettol Handwash Original 250 ml"),
    ("RB100102", "Dettol", "Health & Hygiene", "Hand Wash", "Dettol Original Antibacterial Liquid Hand Wash 500ml", 500, "ml", 1,
     "DETTOL ANTIBAC HW ORIG 500ML", "Dettol Handwash Original 500 ml"),
    ("RB100103", "Dettol", "Health & Hygiene", "Hand Wash", "Dettol Sensitive Antibacterial Liquid Hand Wash 250ml", 250, "ml", 1,
     "DETTOL ANTIBAC HW SENS 250ML", "Dettol Handwash Sensitive 250ml"),
    ("RB100110", "Dettol", "Health & Hygiene", "Surface Care", "Dettol Antibacterial Surface Cleanser Spray 500ml", 500, "ml", 1,
     "DETTOL SURF CLNR SPR 500ML", "Dettol Surface Cleaner Spray 500ml"),
    ("RB100120", "Dettol", "Health & Hygiene", "Disinfectant", "Dettol Original Disinfectant Liquid 500ml", 500, "ml", 1,
     "DETTOL ORIG DISINF LIQ 500ML", "Dettol Antiseptic Disinfectant Liquid 500 ml"),
    ("RB100121", "Dettol", "Health & Hygiene", "Disinfectant", "Dettol Original Disinfectant Liquid 1L", 1000, "ml", 1,
     "DETTOL ORIG DISINF LIQ 1L", "Dettol Antiseptic Disinfectant Liquid 1 L"),
    ("RB100130", "Dettol", "Health & Hygiene", "Wipes", "Dettol Antibacterial Lemon Surface Wipes 36 Pack", 36, "pc", 1,
     "DETTOL ANTIBAC WIPES LEM 36S", "Dettol Surface Wipes Lemon 36 pcs"),
    ("RB200201", "Finish", "Home Care", "Dishwashing", "Finish All in One Max Dishwasher Tablets 30 Pack", 30, "tab", 1,
     "FINISH AIO MAX 30 TABS", "Finish All In One Max Tablets 30N"),
    ("RB200202", "Finish", "Home Care", "Dishwashing", "Finish All in One Max Dishwasher Tablets 50 Pack", 50, "tab", 1,
     "FINISH AIO MAX 50 TABS", "Finish All In One Max Tablets 50N"),
    ("RB200210", "Finish", "Home Care", "Dishwashing", "Finish Quantum Ultimate Dishwasher Tablets 40 Pack", 40, "tab", 1,
     "FINISH QUANTUM ULT 40 TABS", "Finish Quantum Ultimate 40 Tabs"),
    ("RB200220", "Finish", "Home Care", "Dishwashing", "Finish Rinse Aid Regular 400ml", 400, "ml", 1,
     "FINISH RINSE AID 400ML", "Finish Rinse Aid 400ml"),
    ("RB200301", "Vanish", "Home Care", "Fabric Treatment", "Vanish Oxi Action Fabric Stain Remover Powder 470g", 470, "g", 1,
     "VANISH OXI ACTION PWD 470G", "Vanish Oxi Action Powder 470 g"),
    ("RB200302", "Vanish", "Home Care", "Fabric Treatment", "Vanish Oxi Action Whites Stain Remover Powder 470g", 470, "g", 1,
     "VANISH OXI WHITES PWD 470G", "Vanish Oxi Action Crystal White Powder 470g"),
    ("RB200303", "Vanish", "Home Care", "Fabric Treatment", "Vanish Oxi Action Stain Remover Gel 1L", 1000, "ml", 1,
     "VANISH OXI ACTION GEL 1L", "Vanish Oxi Action Liquid 1 Ltr"),
    ("RB200401", "Harpic", "Home Care", "Toilet Care", "Harpic Power Plus Toilet Cleaner Original 750ml", 750, "ml", 1,
     "HARPIC POWER PLUS ORIG 750ML", "Harpic Power Plus Original 750 ml"),
    ("RB200402", "Harpic", "Home Care", "Toilet Care", "Harpic Power Plus Toilet Cleaner Original 500ml", 500, "ml", 1,
     "HARPIC POWER PLUS ORIG 500ML", "Harpic Power Plus Original 500 ml"),
    ("RB200410", "Harpic", "Home Care", "Toilet Care", "Harpic Bleach Toilet Cleaner Citrus 750ml", 750, "ml", 1,
     "HARPIC BL CLNR CITRUS 750ML", "Harpic Bleach Citrus 750ml"),
    ("RB200501", "Air Wick", "Home Care", "Air Care", "Air Wick Freshmatic Refill Lavender 250ml", 250, "ml", 1,
     "AIR WICK FRESHMATIC REFILL LAV 250ML", "Airwick Freshmatic Refill Lavender 250ml"),
    ("RB200502", "Air Wick", "Home Care", "Air Care", "Air Wick Plug In Refill Lavender 19ml", 19, "ml", 1,
     "AIR WICK PLUG IN REFILL LAV 19ML", "Airwick Electrical Refill Lavender 19 ml"),
    ("RB300601", "Durex", "Sexual Wellbeing", "Condoms", "Durex Extra Safe Condoms 12 Pack", 12, "pc", 1,
     "DUREX EXTRA SAFE 12S", "Durex Extra Safe Condoms 12 pcs"),
    ("RB300602", "Durex", "Sexual Wellbeing", "Condoms", "Durex Thin Feel Condoms 12 Pack", 12, "pc", 1,
     "DUREX THIN FEEL 12S", "Durex Thin Feel Condoms 12 pcs"),
    ("RB300603", "Durex", "Sexual Wellbeing", "Condoms", "Durex Thin Feel Condoms 3 Pack", 3, "pc", 1,
     "DUREX THIN FEEL 3S", "Durex Thin Feel Condoms 3 pcs"),
    ("RB300701", "Strepsils", "OTC Health", "Sore Throat", "Strepsils Honey and Lemon Lozenges 24 Pack", 24, "pc", 1,
     "STREPSILS HNY & LMN 24S", "Strepsils Honey Lemon Lozenges 24 N"),
    ("RB300702", "Strepsils", "OTC Health", "Sore Throat", "Strepsils Strawberry Sugar Free Lozenges 24 Pack", 24, "pc", 1,
     "STREPSILS STR SUGAR FREE 24S", "Strepsils Strawberry Sugarfree 24 N"),
    ("RB300703", "Strepsils", "OTC Health", "Sore Throat", "Strepsils Honey and Lemon Lozenges 36 Pack", 36, "pc", 1,
     "STREPSILS HNY & LMN 36S", "Strepsils Honey Lemon Lozenges 36 N"),
    ("RB300801", "Gaviscon", "OTC Health", "Digestive", "Gaviscon Double Action Liquid Mint 300ml", 300, "ml", 1,
     "GAVISCON DBL ACTION LIQ 300ML", "Gaviscon Double Action Liquid 300 ml"),
    ("RB300802", "Gaviscon", "OTC Health", "Digestive", "Gaviscon Double Action Tablets Mint 24 Pack", 24, "tab", 1,
     "GAVISCON DBL ACTION 24 TABS", "Gaviscon Double Action Tablets 24 N"),
    ("RB300901", "Nurofen", "OTC Health", "Pain Relief", "Nurofen Express 200mg Liquid Capsules 16 Pack", 16, "cap", 1,
     "NUROFEN EXPRESS 200MG 16 CAPS", "Nurofen Express Liquid Capsules 16 N"),
    ("RB300902", "Nurofen", "OTC Health", "Pain Relief", "Nurofen 200mg Tablets 16 Pack", 16, "tab", 1,
     "NUROFEN 200MG TABS 16S", "Nurofen 200mg Tablets 16 N"),
    ("RB301001", "Veet", "Health & Hygiene", "Hair Removal", "Veet Sensitive Hair Removal Cream 100ml", 100, "ml", 1,
     "VEET SENS HAIR REMOVAL CREAM 100ML", "Veet Hair Removal Cream Sensitive 100 ml"),
]

# A product the retailer sells that the ERP master does not have yet (new launch)
NEW_LAUNCH = ("DETTOL TRU CLEAN SPRAY 500ML", "Dettol Tru Clean Spray 500ml")

UK_STORES = [
    # store_no, tesco_store_name, format, file_address, town, postcode, master_address, master_name
    ("2011", "LEEDS SEACROFT EXTRA", "Extra", "Seacroft Green Shop Ctr", "Leeds", "LS14 6JD", "Seacroft Green Shopping Centre", "TESCO EXTRA LEEDS SEACROFT"),
    ("2012", "LEEDS ROUNDHAY EXP", "Express", "Roundhay Rd", "Leeds", "LS8 5AN", "412 Roundhay Road", "TESCO EXPRESS ROUNDHAY RD LEEDS"),
    ("2013", "LEEDS ROUNDHAY SUPERSTORE", "Superstore", "Roundhay Rd", "Leeds", "LS8 5AN", "380 Roundhay Road", "TESCO SUPERSTORE ROUNDHAY LEEDS"),
    ("2020", "MANCHESTER WEST DIDSBURY", "Superstore", "Princess Pkwy", "Manchester", "M20 2LR", "Princess Parkway, West Didsbury", "TESCO SUPERSTORE WEST DIDSBURY"),
    ("2021", "MANCHESTER ARNDALE METRO", "Metro", "Market St", "Manchester", "M4 3AQ", "Arndale Centre, Market Street", "TESCO METRO MANCHESTER ARNDALE"),
    ("2030", "BIRMINGHAM DUDLEY RD EXT", "Extra", "Dudley Rd", "Birmingham", "B18 7QW", "Dudley Road", "TESCO EXTRA BIRMINGHAM DUDLEY ROAD"),
    ("2031", "BIRMINGHAM PERRY BARR", "Superstore", "Walsall Rd", "Birmingham", "B42 1AA", "One Stop Shopping Centre, Walsall Road", "TESCO PERRY BARR"),
    ("2040", "LONDON KENSINGTON SUPERSTORE", "Superstore", "Cromwell Rd", "London", "W8 6SR", "West Cromwell Road", "TESCO KENSINGTON"),
    ("2041", "LONDON SURREY QUAYS EXT", "Extra", "Redriff Rd", "London", "SE16 7LL", "Redriff Road, Surrey Quays", "TESCO EXTRA SURREY QUAYS"),
    ("2042", "LONDON HOLBORN EXP", "Express", "High Holborn", "London", "WC1V 6BX", "133 High Holborn", "TESCO EXPRESS HOLBORN"),
    ("2050", "BRISTOL EASTVILLE EXTRA", "Extra", "Eastgate Rd", "Bristol", "BS5 6XX", "Eastgate Centre, Eastgate Road", "TESCO EXTRA EASTVILLE"),
    ("2051", "BRISTOL GOLDEN HILL", "Superstore", "Golden Hill", "Bristol", "BS7 8XA", "Golden Hill, Henleaze", "TESCO SUPERSTORE GOLDEN HILL"),
    ("2060", "GLASGOW SILVERBURN EXT", "Extra", "Barrhead Rd", "Glasgow", "G53 6AG", "Silverburn Centre, Barrhead Road", "TESCO EXTRA SILVERBURN"),
    ("2061", "GLASGOW MARYHILL", "Superstore", "Maryhill Rd", "Glasgow", "G20 9SH", "Maryhill Shopping Centre", "TESCO MARYHILL"),
    ("2070", "CARDIFF EXTRA", "Extra", "Excelsior Rd", "Cardiff", "CF14 3AT", "Excelsior Road, Western Avenue", "TESCO EXTRA CARDIFF WESTERN AVE"),
    ("2071", "CARDIFF CITY CENTRE EXP", "Express", "Queen St", "Cardiff", "CF10 2BH", "Queen Street", "TESCO EXPRESS QUEEN STREET CARDIFF"),
    ("2080", "NEWCASTLE KINGSTON PARK", "Extra", "Kingston Park Rd", "Newcastle upon Tyne", "NE3 2FP", "Kingston Park Road", "TESCO EXTRA KINGSTON PARK"),
    ("2081", "NOTTINGHAM WEST BRIDGFORD", "Superstore", "Rushcliffe Rd", "Nottingham", "NG2 6SJ", "Rushcliffe Retail Park", "TESCO WEST BRIDGFORD"),
    # postcode typo in the retailer file (LS1 vs LS11) -> must fall back to city + address
    ("2090", "LEEDS WHITE ROSE", "Extra", "Dewsbury Rd", "Leeds", "LS1 8LU", "White Rose Centre, Dewsbury Road", "TESCO EXTRA WHITE ROSE"),
    # new store, not in the RB customer master yet -> unmatched, goes to review
    ("2099", "YORK CLIFTON MOOR EXT", "Extra", "Stirling Rd", "York", "YO30 4XZ", None, None),
]
UK_TYPO_TRUE_POSTCODE = {"2090": "LS11 8LU"}

UK_OTHER_BANNER = [
    ("UKS0001", "SAINSBURYS SEACROFT", "Sainsbury's", "Seacroft Green Shopping Centre", "Leeds", "LS14 6JD"),
    ("UKS0002", "SAINSBURYS KENSINGTON", "Sainsbury's", "Cromwell Road", "London", "W8 6SR"),
]

IN_STORES = [
    # site_code, site_name, city, pin, master_name, master_address
    ("D045", "DMART POWAI", "Mumbai", "400076", "Avenue Supermarts Ltd - Powai", "Hiranandani Gardens, Powai"),
    ("D046", "DMART THANE WEST", "Thane", "400601", "Avenue Supermarts Ltd - Thane West", "Ghodbunder Road, Thane West"),
    ("D047", "DMART BORIVALI", "Mumbai", "400092", "Avenue Supermarts Ltd - Borivali", "SV Road, Borivali West"),
    ("D101", "DMART KHARADI", "Pune", "411014", "Avenue Supermarts Ltd - Kharadi", "Nagar Road, Kharadi"),
    ("D102", "DMART HADAPSAR", "Pune", "411028", "Avenue Supermarts Ltd - Hadapsar", "Solapur Road, Hadapsar"),
    ("D201", "DMART WHITEFIELD", "Bengaluru", "560066", "Avenue Supermarts Ltd - Whitefield", "ITPL Main Road, Whitefield"),
    ("D202", "DMART BANNERGHATTA", "Bangalore", "560076", "Avenue Supermarts Ltd - Bannerghatta Rd", "Bannerghatta Road"),
    ("D301", "DMART KUKATPALLY", "Hyderabad", "500072", "Avenue Supermarts Ltd - Kukatpally", "KPHB Colony, Kukatpally"),
    ("D302", "DMART AMEERPET", "Hyderabad", "500016", "Avenue Supermarts Ltd - Ameerpet", "SR Nagar, Ameerpet"),
    ("D401", "DMART VADODARA", "Vadodara", "390007", "Avenue Supermarts Ltd - Alkapuri", "RC Dutt Road, Alkapuri"),
    ("D402", "DMART AHMEDABAD SG HWY", "Ahmedabad", "380054", "Avenue Supermarts Ltd - SG Highway", "SG Highway, Bodakdev"),
    ("D501", "DMART CHENNAI OMR", "Chennai", "600097", "Avenue Supermarts Ltd - OMR", "Old Mahabalipuram Road, Thoraipakkam"),
]


def build(root: str | Path, seed: int = 7) -> dict[str, Path]:
    rng = random.Random(seed)
    root = Path(root)
    for sub in ["inbound/UK/TESCO", "inbound/IN/DMART", "masters", "existing_bronze", "ground_truth", "veritas"]:
        (root / sub).mkdir(parents=True, exist_ok=True)

    # ── ERP product master ──────────────────────────────────────────────────
    erp_rows, eans = [], {}
    for i, p in enumerate(PRODUCTS):
        code, brand, cat, sub, desc, size, unit, pack = p[:8]
        e = ean13(f"5000{158:03d}{100000 + i * 37:06d}"[:12])
        eans[code] = e
        erp_rows.append({
            "rb_product_code": code, "ean_code": e, "product_description": desc,
            "global_brand": brand, "global_category": cat, "global_sub_category": sub,
            "pack_size": size, "pack_unit": unit, "pack_count": pack, "status": "ACTIVE",
        })
    erp = pd.DataFrame(erp_rows)
    erp.to_csv(root / "masters/erp_product_master.csv", index=False)

    # ── RB customer master ──────────────────────────────────────────────────
    cust_rows = []
    for i, s in enumerate(UK_STORES):
        store_no, _, fmt, _, town, pc, m_addr, m_name = s
        if m_addr is None:
            continue
        cust_rows.append({
            "rb_outlet_code": f"UKT{1000 + i:04d}", "customer_name": m_name, "banner": "Tesco",
            "market_code": "UK", "address_line": m_addr, "city": town,
            "postcode": UK_TYPO_TRUE_POSTCODE.get(store_no, pc),
            "channel": {"Express": "Convenience", "Metro": "Convenience"}.get(fmt, "Hypermarket"),
            "region": town,
        })
    for code, name, banner, addr, town, pc in UK_OTHER_BANNER:
        cust_rows.append({"rb_outlet_code": code, "customer_name": name, "banner": banner, "market_code": "UK",
                          "address_line": addr, "city": town, "postcode": pc, "channel": "Hypermarket", "region": town})
    for i, s in enumerate(IN_STORES):
        site, _, city, pin, m_name, m_addr = s
        cust_rows.append({
            "rb_outlet_code": f"INDM{200 + i:04d}", "customer_name": m_name, "banner": "DMart",
            "market_code": "IN", "address_line": m_addr, "city": {"Bangalore": "Bengaluru"}.get(city, city),
            "postcode": pin, "channel": "Modern Trade", "region": city,
        })
    cust = pd.DataFrame(cust_rows)
    cust.to_csv(root / "masters/rb_customer_master.csv", index=False)

    # ground truth outlet xref
    outlet_truth = []
    ci = 0
    for i, s in enumerate(UK_STORES):
        outlet_truth.append({"market": "UK", "retailer": "TESCO", "retailer_outlet_code": s[0],
                             "rb_outlet_code": f"UKT{1000 + i:04d}" if s[6] else None})
    for i, s in enumerate(IN_STORES):
        outlet_truth.append({"market": "IN", "retailer": "DMART", "retailer_outlet_code": s[0],
                             "rb_outlet_code": f"INDM{200 + i:04d}"})
    pd.DataFrame(outlet_truth).to_csv(root / "ground_truth/outlet_xref.csv", index=False)
    del ci

    # ── Tesco file ──────────────────────────────────────────────────────────
    tesco_products = []
    for i, p in enumerate(PRODUCTS):
        code = p[0]
        tpnb = str(50_000_000 + i * 1171)
        e = eans[code]
        r = rng.random()
        if r < 0.15:
            barcode = ""                         # missing barcode
        elif r < 0.25:
            barcode = e.lstrip("5")[:12]         # truncated / wrong length
        else:
            barcode = e
        tesco_products.append((tpnb, p[8], barcode, code))
    tesco_products.append(("59999001", NEW_LAUNCH[0], "5000158999990", None))

    weeks = [date(2026, 9, 26), date(2026, 10, 3)]   # Saturdays
    sales = []
    for wk in weeks:
        for s in UK_STORES:
            for tp in tesco_products:
                if rng.random() < 0.35:
                    continue
                units = rng.randint(1, 60)
                price = rng.choice([1.5, 2.25, 3.0, 4.5, 6.0, 8.75, 11.0])
                sales.append({
                    "Week Ending": wk.strftime("%d/%m/%Y"), "TPNB": tp[0], "Product Description": tp[1],
                    "Barcode": tp[2], "Store No": s[0], "Store Name": s[1], "Sales Units": units,
                    "Sales Value (£)": round(units * price, 2), "Promo Indicator": rng.choice(["Y", "N", "N", "N"]),
                })
    sales_df = pd.DataFrame(sales)
    stores_df = pd.DataFrame([{
        "Store No": s[0], "Store Name": s[1], "Format": s[2], "Address": s[3], "Town": s[4], "Post Code": s[5],
    } for s in UK_STORES])

    tesco_path = root / "inbound/UK/TESCO/tesco_weekly_epos_2026w40.xlsx"
    with pd.ExcelWriter(tesco_path, engine="openpyxl") as xw:
        title = pd.DataFrame([["Tesco Partner Toolkit - Weekly EPOS Report"],
                              ["Supplier: Reckitt Benckiser UK Ltd | Generated 2026-10-05"], [None]])
        title.to_excel(xw, sheet_name="Sales", index=False, header=False, startrow=0)
        sales_df.to_excel(xw, sheet_name="Sales", index=False, startrow=3)
        total = pd.DataFrame([["Total", None, None, None, None, None, int(sales_df["Sales Units"].sum()),
                               round(float(sales_df["Sales Value (£)"].sum()), 2), None]])
        total.to_excel(xw, sheet_name="Sales", index=False, header=False, startrow=4 + len(sales_df))
        stores_df.to_excel(xw, sheet_name="Stores", index=False)

    # ── DMart file ──────────────────────────────────────────────────────────
    dmart_products = []
    for i, p in enumerate(PRODUCTS[:24]):
        code = p[0]
        e = eans[code]
        r = rng.random()
        if r < 0.25:
            ean_txt = f"{float(e):.5E}".replace("E+", "E+")    # Excel scientific notation, digits lost
        elif r < 0.40:
            ean_txt = ""
        else:
            ean_txt = e
        dmart_products.append((f"ART{700100 + i * 13}", p[9], ean_txt, code))
    dmart_products.append(("ART799999", NEW_LAUNCH[1], "", None))

    wk = date(2026, 10, 4)  # Sunday
    dm = []
    for s in IN_STORES:
        for dp in dmart_products:
            if rng.random() < 0.3:
                continue
            q = rng.randint(2, 120)
            dm.append({
                "Bill Week": wk.strftime("%d-%m-%Y"), "Article Code": dp[0], "Article Desc": dp[1],
                "EAN No": dp[2], "Site Code": s[0], "Site Name": s[1], "City": s[2], "Pin Code": s[3],
                "Qty": q, "Net Sales INR": round(q * rng.choice([49, 99, 145, 199, 249, 399]), 2),
            })
    dm_df = pd.DataFrame(dm)
    dmart_path = root / "inbound/IN/DMART/dmart_sellout_2026-10-04.csv"
    dm_df.to_csv(dmart_path, index=False)

    # ground truth product xref
    prod_truth = [{"market": "UK", "retailer": "TESCO", "retailer_sku_code": t[0], "rb_product_code": t[3]}
                  for t in tesco_products]
    prod_truth += [{"market": "IN", "retailer": "DMART", "retailer_sku_code": d[0], "rb_product_code": d[3]}
                   for d in dmart_products]
    pd.DataFrame(prod_truth).to_csv(root / "ground_truth/product_xref.csv", index=False)

    # ── Tesco Toolkit daily file: the layout Veritas mapping #4285 already handles ──
    fixture = Path(__file__).resolve().parent / "fixtures" / "veritas_4285_tesco_toolkit_daily.json"
    (root / "veritas" / fixture.name).write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    daily_cols = ["TPNB", "TPNB Description", "Store ID", "Store Name", "Supplier", "Calendar Date", "Sales Value TY",
                  "Sales % Var LW", "LFL % TY", "Sales Volume TY", "Sales % Var LW (Vol)", "COGS Value TY",
                  "RTC Waste Value TY", "OOC Waste Value TY", "DAM Waste Value TY", "Waste % TY", "Stock Volume TY",
                  "Days Cover TY (Vol)", "Days Cover Var LW (Vol)", "CSL % TY (Vol)"]
    daily = []
    for day in range(5, 12):                     # 2026-10-05 .. 2026-10-11
        d = date(2026, 10, day)
        for s in UK_STORES[:12]:
            for tp in tesco_products[:20]:
                if rng.random() < 0.55:
                    continue
                vol = rng.randint(1, 12)
                val = round(vol * rng.choice([1.5, 2.25, 3.0, 4.5, 6.0]), 2)
                daily.append({
                    "TPNB": tp[0], "TPNB Description": tp[1], "Store ID": s[0], "Store Name": s[1],
                    "Supplier": "RECKITT BENCKISER", "Calendar Date": d.isoformat(), "Sales Value TY": val,
                    "Sales % Var LW": round(rng.uniform(-20, 20), 1), "LFL % TY": round(rng.uniform(-10, 10), 1),
                    "Sales Volume TY": vol, "Sales % Var LW (Vol)": round(rng.uniform(-20, 20), 1),
                    "COGS Value TY": round(val * 0.6, 2), "RTC Waste Value TY": 0, "OOC Waste Value TY": 0,
                    "DAM Waste Value TY": 0, "Waste % TY": 0, "Stock Volume TY": rng.randint(0, 40),
                    "Days Cover TY (Vol)": round(rng.uniform(1, 20), 1), "Days Cover Var LW (Vol)": 0,
                    "CSL % TY (Vol)": round(rng.uniform(90, 100), 1),
                })
    daily_df = pd.DataFrame(daily, columns=daily_cols)
    daily_path = root / "inbound/UK/TESCO/tesco_toolkit_daily_2026-10-11.csv"
    daily_df.to_csv(daily_path, index=False, sep=";")

    # ── Existing bronze: what Veritas #4285 loaded into POS_SELLOUT, with planted differences ──
    legacy = pd.DataFrame({
        "pos_date": daily_df["Calendar Date"].str.replace("-", "", regex=False),
        "legal_entity_code": "IGB0201", "mgmt_entity_code": "8231",
        "retailer_outlet_code": daily_df["Store ID"] + "_Tesco_Daily",
        "retailer_product_code": daily_df["TPNB"] + "_Tesco",
        "transaction_id": "2", "currency_code": "GBP", "uom": "EA",
        "quantity": daily_df["Sales Volume TY"], "amount": daily_df["Sales Value TY"],
    })
    # Planted: the current load is missing store 2021 on 2026-10-07 (file arrived late, never reloaded)
    legacy = legacy[~((legacy.retailer_outlet_code == "2021_Tesco_Daily") & (legacy.pos_date == "20261007"))]
    # Planted: one row was loaded with a stale quantity
    legacy = legacy.reset_index(drop=True)
    legacy.loc[0, "quantity"] = int(legacy.loc[0, "quantity"]) + 5
    # Out of scope: another retailer in the same legal entity, and the same retailer last month
    legacy = pd.concat([legacy, pd.DataFrame([
        {"pos_date": "20261008", "legal_entity_code": "IGB0201", "mgmt_entity_code": "8231",
         "retailer_outlet_code": "S1_Sainsburys", "retailer_product_code": "7001_Sainsburys", "transaction_id": "2",
         "currency_code": "GBP", "uom": "EA", "quantity": 5, "amount": 10.0},
        {"pos_date": "20260905", "legal_entity_code": "IGB0201", "mgmt_entity_code": "8231",
         "retailer_outlet_code": "2011_Tesco_Daily", "retailer_product_code": "50000000_Tesco", "transaction_id": "2",
         "currency_code": "GBP", "uom": "EA", "quantity": 3, "amount": 4.5}])], ignore_index=True)
    # Write it the way Reckitt's bronze table looks (target sample shared 2026-10-22)
    phys = legacy.rename(columns={"pos_date": "DATE", "mgmt_entity_code": "MANAGEMENT_ENTITY_CODE"})
    phys.columns = [c.upper() for c in phys.columns]
    for c in ["SUPPLIER_CODE", "SUPPLIER_NAME", "TRANSACTION_LINE_ITEM"] + [f"MEASURE_{i}" for i in range(1, 21)] + \
            [f"ATTRIBUTE_{i}" for i in range(1, 6)]:
        phys[c] = "null"
    phys["SUBMISSION_ID"] = "15267880-5fe5-470e-9d1a-0c3b7c2f4e11"
    phys["RETAILER"] = phys["RETAILER_PRODUCT_CODE"].str.split("_").str[-1].str.upper()
    phys["CURRENT_STATUS"] = "I"
    phys["ROW_INSERT_TS"] = "2026-10-12T06:15:09.196Z"
    order = (["DATE", "LEGAL_ENTITY_CODE", "MANAGEMENT_ENTITY_CODE", "RETAILER_OUTLET_CODE", "RETAILER_PRODUCT_CODE",
              "SUPPLIER_CODE", "SUPPLIER_NAME", "TRANSACTION_ID", "TRANSACTION_LINE_ITEM", "CURRENCY_CODE", "UOM",
              "QUANTITY", "AMOUNT"] + [f"MEASURE_{i}" for i in range(1, 6)] +
             ["SUBMISSION_ID", "RETAILER", "CURRENT_STATUS", "ROW_INSERT_TS"] +
             [f"MEASURE_{i}" for i in range(6, 21)] + [f"ATTRIBUTE_{i}" for i in range(1, 6)])
    phys[order].to_csv(root / "existing_bronze/bronze.pos_sellout.csv", index=False, sep="\t")

    return {"tesco": tesco_path, "tesco_daily": daily_path, "dmart": dmart_path, "root": root}


if __name__ == "__main__":
    import sys
    out = build(sys.argv[1] if len(sys.argv) > 1 else "./data")
    print({k: str(v) for k, v in out.items()})
