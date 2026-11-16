import argparse
import csv
import glob
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ==============================================================================
# CONFIGURATION & CLI ARGUMENTS
# ==============================================================================
parser = argparse.ArgumentParser(description="Audit Claude session token usage and API-equivalent costs.")
parser.add_argument(
    "project",
    nargs="?",
    default=None,
    help="Project folder or keyword to filter (omit or pass --all to audit all projects).",
)
parser.add_argument(
    "--all",
    action="store_true",
    help="Audit all projects under ~/.claude/projects/ instead of filtering to one.",
)
parser.add_argument(
    "--tier",
    choices=["Pro", "Max", "Ultra"],
    default="Pro",
    help="Subscription tier for value comparison (default: Pro).",
)
parser.add_argument(
    "--weekly-pct",
    type=float,
    default=None,
    help="Current weekly quota percentage from `/usage` (e.g. 42.5) to estimate quota share.",
)
args = parser.parse_args()

TARGET_PROJECT = None if args.all else args.project
SUBSCRIPTION_TIER = args.tier
CURRENT_WEEKLY_PCT = args.weekly_pct

PLAN_PRICES = {
    "Pro": 20.0,
    "Max": 100.0,
    "Ultra": 200.0,
}

# Standard Anthropic API rates in USD per 1 Million tokens (2026 list prices)
# Format: (base_input, output, cache_read)
PRICING = {
    "claude-opus-5-5":   (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5":  (0.10,  0.50, 0.01),
    "claude-fable-5-2":  (8.00, 40.00, 0.20),
    "claude-fable-5-1":  (10.00, 50.00, 0.25),
    "claude-opus-5":     (5.00, 25.00, 0.50),
    "claude-sonnet-5":   (2.00, 10.00, 0.20),
    "claude-opus-4-6":   (5.00, 25.00, 0.50),
    "claude-opus-4-5":   (5.00, 25.00, 0.50),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30),
    "claude-haiku-4-5":  (1.00,  5.00, 0.10),
}

# ==============================================================================
# HELPERS
# ==============================================================================
def get_rates(model_name: str, total_prompt_tokens: int = 0):
    m = (model_name or "").lower()
    if "haiku-5-5" in m and total_prompt_tokens > 100_000:
        return (0.50, 2.50, 0.05)
    for prefix, rates in PRICING.items():
        if prefix in m:
            return rates
    if "opus" in m:
        return (4.00, 20.00, 0.20)
    if "haiku" in m:
        return (0.10, 0.50, 0.01)
    return (2.00, 10.00, 0.10)


def parse_timestamp(ts_str: str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_text_from_content(content):
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(" ".join(parts).split())
    return ""


def load_session_title_map(claude_root: str):
    title_map = {}
    for idx_path in glob.glob(os.path.join(claude_root, "**", "sessions-index.json"), recursive=True):
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                idx_data = json.load(f)
            entries = idx_data.get("entries", idx_data if isinstance(idx_data, list) else [])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("sessionId") or entry.get("id")
                title = (
                    entry.get("customTitle")
                    or entry.get("summary")
                    or entry.get("title")
                    or entry.get("firstPrompt")
                )
                if sid and title:
                    title_map[sid] = " ".join(str(title).split())
        except Exception:
            continue
    return title_map

# ==============================================================================
# SCAN ALL PROJECTS & SESSIONS
# ==============================================================================
claude_base = os.path.expanduser("~/.claude")
projects_root = os.path.join(claude_base, "projects")
index_titles = load_session_title_map(claude_base)

all_files = sorted(
    glob.glob(os.path.join(projects_root, "**", "*.jsonl"), recursive=True),
    key=os.path.getmtime,
)

if not all_files:
    print(f"No .jsonl files found under {projects_root}")
    raise SystemExit(1)

now_utc = datetime.now(timezone.utc)
seven_days_ago = now_utc - timedelta(days=7)

matched_rows = []
seen_message_ids = set()
project_breakdown = defaultdict(float)

totals = {
    "target_all_cost": 0.0,
    "target_main_cost": 0.0,
    "target_sub_cost": 0.0,
    "target_all_tokens": 0,
    "target_week_cost": 0.0,
    "other_week_cost": 0.0,
    "other_all_cost": 0.0,
}

target_lower = TARGET_PROJECT.lower() if TARGET_PROJECT else None

for filepath in all_files:
    rel_path = os.path.relpath(filepath, projects_root)
    path_parts = rel_path.split(os.sep)
    project_folder = path_parts[0]
    file_stem, _ = os.path.splitext(os.path.basename(filepath))
    is_subagent = ("subagents" in path_parts) or file_stem.startswith("agent-")

    parent_sid = path_parts[-3] if ("subagents" in path_parts and len(path_parts) >= 3) else None
    matched_target = True if not target_lower else (target_lower in rel_path.lower())

    s_cost = 0.0
    s_week_cost = 0.0
    s_in = 0
    s_out = 0
    s_cw_5m = 0
    s_cw_1h = 0
    s_cr = 0
    models_used = set()
    first_dt = None

    custom_title = None
    summary_title = None
    index_title = index_titles.get(file_stem)
    first_prompt = None
    session_slug = None

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            cwd = data.get("cwd", "")
            if target_lower and cwd and target_lower in cwd.lower():
                matched_target = True

            dt = parse_timestamp(data.get("timestamp"))
            if dt and (first_dt is None or dt < first_dt):
                first_dt = dt

            rec_type = data.get("type")
            if rec_type == "custom-title" and data.get("customTitle"):
                custom_title = " ".join(str(data["customTitle"]).split())
            elif rec_type == "summary" and data.get("summary"):
                summary_title = " ".join(str(data["summary"]).split())

            if not session_slug and data.get("slug"):
                session_slug = str(data["slug"])

            msg = data.get("message", {})

            if not first_prompt and rec_type == "user" and not data.get("isMeta"):
                txt = extract_text_from_content(msg.get("content"))
                if txt and not txt.startswith("<command-name>"):
                    first_prompt = txt

            msg_id = msg.get("id")
            usage = msg.get("usage")
            model = msg.get("model", "")

            if usage and msg_id and msg_id not in seen_message_ids:
                seen_message_ids.add(msg_id)
                if model:
                    models_used.add(model.replace("claude-", ""))

                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                c_read = usage.get("cache_read_input_tokens", 0)

                cache_detail = usage.get("cache_creation", {})
                if isinstance(cache_detail, dict) and (
                    "ephemeral_5m_input_tokens" in cache_detail
                    or "ephemeral_1h_input_tokens" in cache_detail
                ):
                    cw_5m = cache_detail.get("ephemeral_5m_input_tokens", 0)
                    cw_1h = cache_detail.get("ephemeral_1h_input_tokens", 0)
                else:
                    cw_5m = usage.get("cache_creation_input_tokens", 0)
                    cw_1h = 0

                prompt_total = inp + cw_5m + cw_1h + c_read
                r_in, r_out, r_cr = get_rates(model, prompt_total)

                msg_cost = (
                    (inp * r_in)
                    + (out * r_out)
                    + (cw_5m * (r_in * 1.25))
                    + (cw_1h * (r_in * 2.00))
                    + (c_read * r_cr)
                ) / 1_000_000

                s_cost += msg_cost
                s_in += inp
                s_out += out
                s_cw_5m += cw_5m
                s_cw_1h += cw_1h
                s_cr += c_read

                msg_dt = dt or datetime.fromtimestamp(os.path.getmtime(filepath), tz=timezone.utc)
                if msg_dt >= seven_days_ago:
                    s_week_cost += msg_cost

    total_s_tokens = s_in + s_out + s_cw_5m + s_cw_1h + s_cr
    if total_s_tokens == 0:
        continue

    resolved_title = (
        custom_title
        or summary_title
        or index_title
        or first_prompt
        or session_slug
        or file_stem
    )

    if is_subagent and parent_sid and parent_sid in index_titles:
        resolved_title = f"[{index_titles[parent_sid]}] {resolved_title}"

    project_breakdown[project_folder] += s_cost

    if matched_target:
        totals["target_all_cost"] += s_cost
        totals["target_all_tokens"] += total_s_tokens
        totals["target_week_cost"] += s_week_cost
        if is_subagent:
            totals["target_sub_cost"] += s_cost
        else:
            totals["target_main_cost"] += s_cost

        date_str = (
            first_dt.strftime("%Y-%m-%d")
            if first_dt
            else datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d")
        )

        matched_rows.append({
            "date": date_str,
            "project": project_folder,
            "type": "Subagent" if is_subagent else "Thread",
            "title": resolved_title,
            "models": ", ".join(sorted(models_used)),
            "input_tokens": s_in,
            "output_tokens": s_out,
            "cache_write_5m": s_cw_5m,
            "cache_write_1h": s_cw_1h,
            "cache_read": s_cr,
            "total_tokens": total_s_tokens,
            "api_cost_usd": round(s_cost, 6),
            "last_7d_cost_usd": round(s_week_cost, 6),
            "file": rel_path,
        })
    else:
        totals["other_all_cost"] += s_cost
        totals["other_week_cost"] += s_week_cost

# ==============================================================================
# COMPUTE SUMMARY METRICS & PRINT REPORT
# ==============================================================================
scope_label = TARGET_PROJECT if TARGET_PROJECT else "ALL PROJECTS"
safe_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", scope_label.lower()).strip("_") or "all_projects"

plan_tier = SUBSCRIPTION_TIER if SUBSCRIPTION_TIER in PLAN_PRICES else "Pro"
monthly_price = PLAN_PRICES[plan_tier]
weekly_amortized = monthly_price / 4.33

thread_count = sum(1 for r in matched_rows if r["type"] == "Thread")
subagent_count = sum(1 for r in matched_rows if r["type"] == "Subagent")

all_projects_all_time = totals["target_all_cost"] + totals["other_all_cost"]
all_projects_week = totals["target_week_cost"] + totals["other_week_cost"]
share_of_all_time = (totals["target_all_cost"] / all_projects_all_time * 100) if all_projects_all_time else 0.0
share_of_week = (totals["target_week_cost"] / all_projects_week * 100) if all_projects_week else 0.0
all_time_value_ratio = totals["target_all_cost"] / monthly_price
last_7d_value_ratio = totals["target_week_cost"] / weekly_amortized

print("\n=== PROJECT COST OVERVIEW ===")
for folder, cost in sorted(project_breakdown.items(), key=lambda x: x[1], reverse=True):
    marker = " <-- SELECTED" if (not target_lower or target_lower in folder.lower()) else ""
    print(f"  ${cost:>9.4f} | {folder}{marker}")

print(f"\n=== {scope_label} SESSION & SUBAGENT BREAKDOWN ===")
print(f"{'Date':<10} | {'Type':<8} | {'Tokens':>11} | {'All-Time':>9} | {'Last 7d':>8} | Full Session / Subagent Title")
print("-" * 110)

for r in matched_rows:
    print(
        f"{r['date']:<10} | {r['type']:<8} | {r['total_tokens']:>11,} | "
        f"${r['api_cost_usd']:>8.4f} | ${r['last_7d_cost_usd']:>7.4f} | {r['title']}"
    )

print("-" * 110)
print(f"Main Threads ({thread_count}):      ${totals['target_main_cost']:>9.4f}")
print(f"Subagents    ({subagent_count}):      ${totals['target_sub_cost']:>9.4f}")
print(f"{scope_label} All-Time:   ${totals['target_all_cost']:>9.4f}  ({totals['target_all_tokens']:,} tokens)")
print(f"{scope_label} Last 7d:    ${totals['target_week_cost']:>9.4f}")

print(f"\n=== SUBSCRIPTION VALUE & QUOTA SHARE ({plan_tier.upper()} - ${monthly_price:.0f}/mo) ===")
print(f"All Projects (All-Time API Equiv): ${all_projects_all_time:>9.4f}  ({scope_label} = {share_of_all_time:.1f}%)")
print(f"All Projects (Last 7d API Equiv):  ${all_projects_week:>9.4f}  ({scope_label} = {share_of_week:.1f}%)")
print(f"All-Time Value Ratio ({plan_tier}):      {all_time_value_ratio:.2f}x your ${monthly_price:.0f}/mo subscription fee")
print(f"Last 7d Value Ratio ({plan_tier}):       {last_7d_value_ratio:.2f}x your ~${weekly_amortized:.2f}/wk amortized cost")

summary_rows = [
    {"Category": "Breakdown", "Metric": f"Main Threads ({thread_count})", "Value": round(totals["target_main_cost"], 4), "Unit": "USD", "Notes": ""},
    {"Category": "Breakdown", "Metric": f"Subagents ({subagent_count})", "Value": round(totals["target_sub_cost"], 4), "Unit": "USD", "Notes": ""},
    {"Category": "Breakdown", "Metric": f"{scope_label} All-Time Cost", "Value": round(totals["target_all_cost"], 4), "Unit": "USD", "Notes": f"{totals['target_all_tokens']:,} total tokens"},
    {"Category": "Breakdown", "Metric": f"{scope_label} Last 7d Cost", "Value": round(totals["target_week_cost"], 4), "Unit": "USD", "Notes": ""},
    {"Category": "Subscription", "Metric": "Subscription Tier", "Value": plan_tier, "Unit": "Plan", "Notes": f"${monthly_price:.2f}/mo (~${weekly_amortized:.2f}/wk)"},
    {"Category": "Subscription", "Metric": "All Projects (All-Time API Equiv)", "Value": round(all_projects_all_time, 4), "Unit": "USD", "Notes": f"{scope_label} = {share_of_all_time:.1f}%"},
    {"Category": "Subscription", "Metric": "All Projects (Last 7d API Equiv)", "Value": round(all_projects_week, 4), "Unit": "USD", "Notes": f"{scope_label} = {share_of_week:.1f}%"},
    {"Category": "Subscription", "Metric": f"All-Time Value Ratio ({plan_tier})", "Value": round(all_time_value_ratio, 2), "Unit": "x monthly fee", "Notes": f"vs ${monthly_price:.0f}/mo"},
    {"Category": "Subscription", "Metric": f"Last 7d Value Ratio ({plan_tier})", "Value": round(last_7d_value_ratio, 2), "Unit": "x weekly cost", "Notes": f"vs ~${weekly_amortized:.2f}/wk amortized"},
]

if CURRENT_WEEKLY_PCT and CURRENT_WEEKLY_PCT > 0 and all_projects_week > 0:
    implied_weekly_budget = all_projects_week / (CURRENT_WEEKLY_PCT / 100.0)
    weekly_quota_pct = (totals["target_week_cost"] / implied_weekly_budget) * 100.0
    print(f"\nCalibrated Weekly Quota Estimate (at {CURRENT_WEEKLY_PCT:.1f}% total usage):")
    print(f"  - Implied Weekly API Cap:      ~${implied_weekly_budget:.2f}/week")
    print(f"  - {scope_label} Weekly Quota Used: ~{weekly_quota_pct:.1f}% of your {plan_tier} weekly limit")
    summary_rows.append({"Category": "Quota Calibration", "Metric": "Implied Weekly API Cap", "Value": round(implied_weekly_budget, 2), "Unit": "USD/week", "Notes": f"At {CURRENT_WEEKLY_PCT:.1f}% total usage"})
    summary_rows.append({"Category": "Quota Calibration", "Metric": f"{scope_label} Weekly Quota Used", "Value": round(weekly_quota_pct, 1), "Unit": "%", "Notes": f"Of {plan_tier} weekly limit"})

# ==============================================================================
# EXPORT TWO-TAB EXCEL (.xlsx) + CSV FALLBACK
# ==============================================================================
if matched_rows:
    try:
        import openpyxl
        wb = openpyxl.Workbook()

        ws_sessions = wb.active
        ws_sessions.title = "Sessions & Subagents"
        headers_1 = list(matched_rows[0].keys())
        ws_sessions.append(headers_1)
        for row in matched_rows:
            ws_sessions.append([row[h] for h in headers_1])

        ws_summary = wb.create_sheet(title="Summary & Subscription")
        headers_2 = ["Category", "Metric", "Value", "Unit", "Notes"]
        ws_summary.append(headers_2)
        for row in summary_rows:
            ws_summary.append([row[h] for h in headers_2])

        xlsx_path = f"{safe_slug}_claude_audit.xlsx"
        wb.save(xlsx_path)
        print(f"\nMulti-tab Excel workbook exported to: {os.path.abspath(xlsx_path)}")
    except ImportError:
        csv_sessions = f"{safe_slug}_claude_audit.csv"
        with open(csv_sessions, "w", newline="", encoding="utf-8") as cf:
            writer = csv.DictWriter(cf, fieldnames=list(matched_rows[0].keys()))
            writer.writeheader()
            writer.writerows(matched_rows)

        csv_summary = f"{safe_slug}_claude_summary.csv"
        with open(csv_summary, "w", newline="", encoding="utf-8") as sf:
            writer = csv.DictWriter(sf, fieldnames=["Category", "Metric", "Value", "Unit", "Notes"])
            writer.writeheader()
            writer.writerows(summary_rows)

        print(f"\nNote: Install openpyxl (`pip install openpyxl`) for a single multi-tab .xlsx file.")
        print(f"Exported 2 CSV files instead:\n  1. {os.path.abspath(csv_sessions)}\n  2. {os.path.abspath(csv_summary)}")
