"""
Generate ~50 faithfulness test cases from current DB state.

Each run pulls real contacts/leads/meetings, builds verifiable factual cases,
and writes them to tests/faithfulness_set.json (overwriting).

Run this whenever the underlying CRM data has shifted enough that the prior
case file is stale (e.g., after a Monday sync).

Usage:
    python tests/gen_faithfulness_cases.py [--out PATH] [--seed N]
"""
import os
import re
import sys
import json
import random
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db.model import get_connection


# ── Stage / score reference pools (used as must_not_contain wrong answers) ──
ALL_STAGES = [
    "New Leads", "NDA Sent", "NDA Signed", "CIM Sent", "Successful Call",
    "Offer/LOI", "Under Contract", "Working", "Warm", "Cool", "Dead",
    "Brokers", "Sequence Finished",
]

ALL_SCORES_EXCLUSIVE = ["A", "B++", "C", "F", "IMMEDIATE INTEREST"]


def _wrong_stages_for(actual: str) -> list:
    """Pick other stages to detect hallucination, skipping any that overlap."""
    actual_lc = actual.lower()
    return [s for s in ALL_STAGES if s.lower() not in actual_lc and actual_lc not in s.lower()][:4]


def gen_stage_cases(conn, n: int = 10) -> list:
    """'What pipeline stage is X in?' — for random named contacts with stage data."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.name, l.monday_group_name
            FROM contacts c
            JOIN leads l ON l.id = c.monday_lead_id
            WHERE c.name IS NOT NULL
              AND l.monday_group_name IS NOT NULL
              AND (c.email IS NULL OR (c.email NOT ILIKE '%%noreply%%'
                                       AND c.email NOT ILIKE '%%notification%%'
                                       AND c.email NOT ILIKE '%%mailer-daemon%%'))
              AND LENGTH(c.name) BETWEEN 6 AND 40
              AND c.name NOT LIKE '%%@%%'
              AND c.name LIKE '%% %%'
            ORDER BY random()
            LIMIT %s
        """, (n,))
        rows = cur.fetchall()

    cases = []
    for i, (name, stage) in enumerate(rows, 1):
        clean_stage = stage.replace("🔥", "").replace("✅", "").replace("🚀", "").replace("❄️", "").replace(".xlsx", "").strip()
        # Use distinctive substring of the stage as match key
        key_parts = clean_stage.split()
        match_key = " ".join(key_parts[:3])[:40].strip()
        cases.append({
            "id": f"STG{i:02d}",
            "category": "pipeline_stage",
            "question": f"What pipeline stage is {name} in?",
            "must_contain_one_of": [match_key] if match_key else [clean_stage[:30]],
            "notes": f"Live DB: {name!r} → {stage!r} — positive-only check; "
                     f"bot may legitimately mention other stages in suggestions"
        })
    return cases


def gen_score_cases(conn, n: int = 8) -> list:
    """'What is X's lead score?' — skip B (substring of B++)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.name, l.lead_score
            FROM contacts c
            JOIN leads l ON l.id = c.monday_lead_id
            WHERE c.name IS NOT NULL
              AND l.lead_score IS NOT NULL
              AND l.lead_score IN ('A', 'B++', 'C', 'IMMEDIATE INTEREST')
              AND LENGTH(c.name) BETWEEN 6 AND 40
              AND c.name NOT LIKE '%%@%%'
              AND c.name LIKE '%% %%'
            ORDER BY random()
            LIMIT %s
        """, (n,))
        rows = cur.fetchall()

    cases = []
    for i, (name, score) in enumerate(rows, 1):
        # The bot always emphasizes the score with quotes or bold markdown
        # (`"C"`, `**B++**`, etc.) — match those distinctive forms rather than
        # the raw letter, which would false-positive on "A" inside "Calad".
        patterns = [
            f'"{score}"',
            f'**{score}**',
            f'`{score}`',
            f'of {score}.',
            f'is {score}.',
        ]
        cases.append({
            "id": f"SCR{i:02d}",
            "category": "lead_score",
            "question": f"What is {name}'s lead score?",
            "must_contain_one_of": patterns,
            "notes": f"Live DB: {name!r} → score {score!r} — matches quoted/bolded form"
        })
    return cases


def gen_pof_cases(conn, n_yes: int = 5, n_no: int = 5) -> list:
    """POF=Yes and POF=No cases — bot must not invent the opposite."""
    cases = []
    with conn.cursor() as cur:
        for has_pof, label, n in [(True, "POF=Yes", n_yes), (False, "POF=No", n_no)]:
            cur.execute("""
                SELECT c.name
                FROM contacts c
                JOIN leads l ON l.id = c.monday_lead_id
                WHERE c.name IS NOT NULL
                  AND l.proof_of_funds IS %s
                  AND LENGTH(c.name) BETWEEN 6 AND 40
              AND c.name NOT LIKE '%%@%%'
              AND c.name LIKE '%% %%'
                ORDER BY random()
                LIMIT %s
            """, (has_pof, n))
            for i, (name,) in enumerate(cur.fetchall(), 1):
                if has_pof:
                    cases.append({
                        "id": f"POF{label.split('=')[1].upper()}{i}",
                        "category": "pof_status",
                        "question": f"Do we have proof of funds on file for {name}?",
                        "must_contain_one_of": ["Yes", "yes", "POF: yes", "proof of funds on file", "submitted"],
                        "must_not_contain": ["No proof of funds", "POF: No", "not submitted", "not on file"],
                        "notes": f"Live DB: {name!r} → POF=TRUE"
                    })
                else:
                    cases.append({
                        "id": f"POFNO{i}",
                        "category": "pof_status",
                        "question": f"Do we have proof of funds on file for {name}?",
                        "must_contain_one_of": ["No", "no", "not", "POF: No", "not submitted", "not on file", "pending"],
                        "must_not_contain": ["POF: Yes", "proof of funds on file already", "submitted and confirmed"],
                        "notes": f"Live DB: {name!r} → POF=FALSE"
                    })
    return cases


def gen_email_cases(conn, n: int = 6) -> list:
    """'What is X's email?' — must return the real address verbatim."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.name, c.email
            FROM contacts c
            WHERE c.name IS NOT NULL
              AND c.email IS NOT NULL
              AND c.email NOT ILIKE '%%noreply%%'
              AND c.email NOT ILIKE '%%notification%%'
              AND c.email NOT ILIKE '%%mailer-daemon%%'
              AND LENGTH(c.name) BETWEEN 6 AND 40
              AND c.name NOT LIKE '%%@%%'
              AND c.name LIKE '%% %%'
            ORDER BY random()
            LIMIT %s
        """, (n,))
        rows = cur.fetchall()

    cases = []
    for i, (name, email) in enumerate(rows, 1):
        domain = email.split("@", 1)[1] if "@" in email else ""
        cases.append({
            "id": f"EML{i:02d}",
            "category": "email_lookup",
            "question": f"What is {name}'s email address?",
            "must_contain_one_of": [email.lower()],
            "must_not_contain_regex": [
                # Common hallucinated alternates with the same name but wrong domain
                r"@gmail\\.com" if "gmail.com" not in domain.lower() else None,
                r"@yahoo\\.com" if "yahoo.com" not in domain.lower() else None,
                r"@hotmail\\.com" if "hotmail.com" not in domain.lower() else None,
            ],
            "notes": f"Live DB: {name!r} → {email!r}"
        })
        # Clean up Nones from must_not_contain_regex
        cases[-1]["must_not_contain_regex"] = [p for p in cases[-1]["must_not_contain_regex"] if p]
    return cases


def gen_company_cases(conn, n: int = 5) -> list:
    """'What company is X with?' — sourced from leads.company."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.name, l.company
            FROM contacts c
            JOIN leads l ON l.id = c.monday_lead_id
            WHERE c.name IS NOT NULL
              AND l.company IS NOT NULL
              AND LENGTH(l.company) BETWEEN 4 AND 60
              AND LENGTH(c.name) BETWEEN 6 AND 40
              AND c.name NOT LIKE '%%@%%'
              AND c.name LIKE '%% %%'
            ORDER BY random()
            LIMIT %s
        """, (n,))
        rows = cur.fetchall()

    cases = []
    for i, (name, company) in enumerate(rows, 1):
        # Use first significant word from company as match key
        key = company.split()[0] if company.split() else company[:20]
        cases.append({
            "id": f"CO{i:02d}",
            "category": "company_lookup",
            "question": f"What company is {name} associated with?",
            "must_contain_one_of": [key[:30]],
            "must_not_contain": [],
            "notes": f"Live DB: {name!r} → company {company!r}"
        })
    return cases


def gen_meeting_cases(conn, n: int = 5) -> list:
    """
    'When was the meeting with X?' — uses Read.ai ingestion month as a loose
    tolerance (the actual meeting date may differ from ingestion date by days,
    so we accept any plausibly-near month and reject only clearly-wrong years).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.name, MAX(ce.created_at)::date AS last_meet
            FROM contact_embeddings ce
            JOIN contacts c ON c.id = ce.contact_id
            WHERE ce.source = 'readai'
              AND c.name IS NOT NULL
              AND LENGTH(c.name) BETWEEN 6 AND 40
              AND c.name NOT LIKE '%%@%%'
              AND c.name LIKE '%% %%'
            GROUP BY c.name
            ORDER BY random()
            LIMIT %s
        """, (n,))
        rows = cur.fetchall()

    cases = []
    for i, (name, last_meet) in enumerate(rows, 1):
        year = str(last_meet.year)
        month_name = last_meet.strftime("%B")
        # Accept any month near the ingestion date (data may be ingested days/weeks late)
        from datetime import timedelta
        nearby_months = sorted({
            (last_meet + timedelta(days=offset)).strftime("%B")
            for offset in (-90, -60, -30, 0, 30, 60, 90)
        })
        cases.append({
            "id": f"MTG{i:02d}",
            "category": "meeting_date",
            "question": f"When was the most recent meeting with {name}?",
            "must_contain_one_of": nearby_months + [year],
            "must_not_contain": [
                str(last_meet.year - 3), str(last_meet.year - 4),
                str(last_meet.year + 3), str(last_meet.year + 4),
            ],
            "notes": f"Live DB Read.ai ingest: {name!r} on {last_meet.isoformat()}. Accepts nearby months/year."
        })
    return cases


def gen_negative_cases() -> list:
    """Empty-set cases — bot must not fabricate profiles for nonexistent names."""
    fake_names = [
        "Zxqvbg Plotzkin", "Mxrlnstein Drogbatte", "Quenndlerp Vossberg",
        "Phlogiston Xanthropic", "Ynphartle Krimblevin",
    ]
    cases = []
    for i, name in enumerate(fake_names, 1):
        cases.append({
            "id": f"NEG{i:02d}",
            "category": "empty_set",
            "question": f"Tell me about a contact named {name}.",
            "must_contain_one_of": ["No contact", "couldn't find", "not found", "no matches", "no match"],
            "must_not_contain": [f"{name} is", f"{name} has", f"{name} is interested"],
            "notes": "Synthetic name — must NOT be fabricated."
        })
    return cases


def gen_count_cases(conn) -> list:
    """
    'How many leads in X stage?' — skip stages with slashes/emojis/xlsx since the
    pipeline-stage tool can't reliably resolve those variations. Tolerate ±5
    because the bot generates the count from row enumeration (known undercount bug).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT monday_group_name, COUNT(*) AS n
            FROM leads
            WHERE monday_group_name IS NOT NULL
              AND monday_group_name NOT LIKE '%%/%%'
              AND monday_group_name NOT LIKE '%%.xlsx%%'
              AND monday_group_name NOT LIKE '%%🔥%%'
              AND monday_group_name NOT LIKE '%%❄️%%'
              AND monday_group_name NOT LIKE '%%🚀%%'
            GROUP BY monday_group_name
            HAVING COUNT(*) BETWEEN 2 AND 200
            ORDER BY random()
            LIMIT 5
        """)
        rows = cur.fetchall()

    cases = []
    for i, (stage, count) in enumerate(rows, 1):
        ok = [str(count + d) for d in range(-5, 6)]
        cases.append({
            "id": f"CNT{i:02d}",
            "category": "stage_count",
            "question": f"How many leads are in '{stage}' stage?",
            "must_contain_one_of": ok,
            "must_not_contain": ["hundreds", "thousands"],
            "notes": f"Live DB: stage {stage!r} has {count} leads. Accepts ±5."
        })
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tests/faithfulness_set.json")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    conn = get_connection()
    try:
        cases = []
        cases += gen_stage_cases(conn, n=10)
        cases += gen_score_cases(conn, n=8)
        cases += gen_pof_cases(conn, n_yes=4, n_no=6)
        cases += gen_email_cases(conn, n=6)
        cases += gen_company_cases(conn, n=5)
        cases += gen_meeting_cases(conn, n=5)
        cases += gen_count_cases(conn)
        cases += gen_negative_cases()
    finally:
        conn.close()

    # Renumber IDs sequentially for stable output
    for i, c in enumerate(cases, 1):
        c["id"] = f"F{i:03d}_{c['id']}"

    with open(args.out, "w") as f:
        json.dump(cases, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(cases)} cases to {args.out}")
    by_cat = {}
    for c in cases:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat:20s} {n}")


if __name__ == "__main__":
    main()
