#!/usr/bin/env python3
"""Generate a realistic multi-system raw dataset with tricky characters.

Writes two JSON-line-ish files:
  seed/raw_rows.csv   - what each source system sent (verbatim strings)
  seed/expected.json  - hand-checked expectations for the end-to-end check

Run: python3 scripts/make_seed.py
"""
from __future__ import annotations

import csv
import json
import random
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "seed"
ZW = "​"   # zero-width space
NBSP = " "  # no-break space
IDEOSP = "　"  # ideographic space
DOTLESS = "ı"  # dotless i (casefold-sensitive)
SIGMA = "Σ"  # Greek capital sigma; final sigma folding demo

SUPPLIERS = [
    # (canonical display name, list of raw variants across systems)
    ("ACME GmbH", [
        "ACME GmbH", "ＡＣＭＥ　ＧｍｂＨ", f"ACME{ZW} GmbH", "  acme   gmbh ",
        "ACME  GMBH", "ＡＣＭＥ　ＧＭＢＨ", "Acme GmbH.",
    ]),
    ("Müller GmbH", [
        "Müller GmbH", "MÜLLER GMBH", "Muller GmbH", "Ｍüｌｌｅｒ　GmbH", "Müller GmbH",
    ]),
    ("北京华为技术有限公司", [
        "北京华为技术有限公司", "北京 华为 技术 有限公司", "北京华为技术有限公司" + ZW,
        "北京　华为　技术　有限公司",
    ]),
    ("SOFTWARE AG", [
        "Software AG", "SOFTWARE AG", "Ｓｏｆｔｗａｒｅ　ＡＧ", "software" + NBSP + "ag",
    ]),
    ("ÀÉÎÕÜ Ltd", [
        "ÀÉÎÕÜ Ltd", "AEIOU Ltd", "ＡＥＩＯＵ　Ｌｔｄ",
    ]),
    ("Straße GmbH", [
        "Straße GmbH", "STRASSE GMBH", "STRASSE  GMBH", "Straße" + ZW + " GmbH",
    ]),
    ("O'Reilly & Sons", [
        "O'Reilly & Sons", "O’Reilly & Sons", "O'REILLY & SONS", "O'Reilly  &  Sons",
    ]),
    ("İstanbul Tekstil", [
        "İstanbul Tekstil", "Istanbul Tekstil", "ISTANBUL TEKSTİL", "İSTANBUL TEKSTİL",
    ]),
    ("ΣΟΦΙΑ ΑΕ", [
        "ΣΟΦΙΑ ΑΕ", "σοφια αε", "Σοφια  Αε", "ΣΟΦΙΑ ΑΕ",
    ]),
    ("Empty-ish", ["", " ", "　", ZW]),
]

# Strings that are *supposed* to collide ambiguously under aggressive rules.
AMBIGUOUS = [
    # "AB-C" vs "AB C" is not identical; a rule that strips punctuation+space merges them.
    ("ACME-Corp", "ACME Corp", "ACME_Corp"),
    # dotless i vs i only merges with diacritic stripping + casefold
    ("Diyarbakır", "Diyarbakir", "DIYARBAKIR"),
    # different accents that NFKD strips to the same base
    ("Málaga", "Malaga", "MÀLAGA"),
    # hyphenated vs space for the same legal entity
    ("Shanghai-Huawei", "Shanghai Huawei", "shanghai  huawei"),
]


def main() -> None:
    rng = random.Random(20261117)
    rows: list[dict[str, str]] = []
    counter = 0
    systems = ["ERP", "CRM", "Ledger", "MDM", "VendorPortal", "LegacyAS400"]

    expected_canonical: dict[str, list[str]] = {}
    for name, variants in SUPPLIERS:
        expected_canonical[name] = list(variants)
        for idx, raw in enumerate(variants):
            counter += 1
            rows.append({
                "source_system": systems[counter % len(systems)],
                "source_id": f"SUP-{counter:04d}",
                "raw_value": raw,
                "canonical_hint": name,
            })
        # add a few duplicates across systems, with revisions
        for _ in range(2):
            counter += 1
            raw = rng.choice(variants)
            rows.append({
                "source_system": systems[counter % len(systems)],
                "source_id": f"SUP-{counter:04d}",
                "raw_value": raw,
                "canonical_hint": name,
            })

    for group in AMBIGUOUS:
        for raw in group:
            counter += 1
            rows.append({
                "source_system": systems[counter % len(systems)],
                "source_id": f"AMB-{counter:04d}",
                "raw_value": raw,
                "canonical_hint": "AMBIGUOUS:" + group[0],
            })

    # Bulk realistic volume, with some noise that should NOT merge.
    first = ["Alpha", "Beta", "Gamma", "Delta", "Omega", "Nova", "Orion", "Vega"]
    suffix = ["Logistics", "Systems", "Trading", "Industries", "Holdings", "Partners"]
    used = set()
    for i in range(400):
        base = f"{rng.choice(first)} {rng.choice(suffix)}"
        variant_kind = i % 4
        if variant_kind == 0:
            raw = base
        elif variant_kind == 1:
            raw = base.upper()
        elif variant_kind == 2:
            raw = "  " + base.lower() + "  "
        else:
            raw = base.replace(" ", "  ")
        key = raw
        if key in used:
            continue
        used.add(key)
        counter += 1
        rows.append({
            "source_system": systems[counter % len(systems)],
            "source_id": f"BULK-{counter:04d}",
            "raw_value": raw,
            "canonical_hint": base,
        })

    OUT.mkdir(exist_ok=True)
    csv_path = OUT / "raw_rows.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["source_system", "source_id", "raw_value", "canonical_hint"])
        writer.writeheader()
        writer.writerows(rows)

    example = {
        "ACME GmbH": [
            "ACME GmbH", "ＡＣＭＥ　ＧｍｂＨ", "ACME​ GmbH", "  acme   gmbh ",
            "ＡＣＭＥ　ＧＭＢＨ", "Acme GmbH.",
        ],
        "Müller GmbH": ["Müller GmbH", "MÜLLER GMBH", "Muller GmbH", "Ｍüｌｌｅｒ　GmbH"],
        "北京华为技术有限公司": [
            "北京华为技术有限公司", "北京 华为 技术 有限公司", "北京华为技术有限公司​",
        ],
        "SOFTWARE AG": ["Software AG", "SOFTWARE AG", "Ｓｏｆｔｗａｒｅ　ＡＧ", "software ag"],
    }
    expectations = {
        "row_count": len(rows),
        "example_groups": example,
        "ambiguous_group": list(AMBIGUOUS[0]),
        "notes": [
            "全角与半角、零宽字符、NBSP/表意空格、大小写、首尾空白都保留在原始值中。",
            "“ACME-Corp” / “ACME Corp” / “ACME_Corp” 只有在规则主动去掉标点与下划线时才会合并。",
            "空值/纯空白应进入空键区，不得与正常实体混在一起。",
        ],
    }
    (OUT / "expected.json").write_text(json.dumps(expectations, ensure_ascii=False, indent=2), encoding="utf-8")

    # Human-readable codepoint audit for a handful of raw values.
    audit = []
    for raw in ["ＡＣＭＥ　ＧｍｂＨ", "ACME​ GmbH", "İstanbul Tekstil", "ΣΟΦΙΑ ΑΕ", "software ag"]:
        audit.append({
            "raw": raw,
            "escaped": raw.encode("unicode_escape").decode("ascii"),
            "codepoints": [
                {"char": ch, "codepoint": f"U+{ord(ch):04X}", "name": unicodedata.name(ch, "UNKNOWN")}
                for ch in raw
            ],
        })
    (OUT / "codepoint_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {csv_path} ({len(rows)} rows) and expectations")


if __name__ == "__main__":
    main()
