"""Render the report page (HTML via Jinja) and the PDF (fpdf2)."""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
env.globals.update(cfg=config)

SUB_LABELS = {"consistency": "Consistency", "momentum": "Momentum", "packaging": "Packaging",
              "retention": "Retention", "conversion": "Conversion"}


def fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,}"
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


env.filters["n"] = fmt


def context(job: dict, audit: dict) -> dict:
    return {"job": job, "m": audit["metrics"], "subs": audit["subscores"], "score": audit["score"],
            "nar": audit["narrative"], "tier": job["tier"], "sub_labels": SUB_LABELS,
            "problems_meta": {p["key"]: p for p in audit.get("problems", [])}}


def render_html(job: dict, audit: dict, **extra) -> str:
    return env.get_template("report.html").render(**context(job, audit), **extra)


# ------------------------------------------------------------------ PDF
def _latin(s) -> str:
    s = str(s if s is not None else "")
    for a, b in {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
                 "…": "...", "→": "->", " ": " ", "•": "-"}.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


class _PDF(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", size=8)
        self.set_text_color(130)
        self.cell(0, 6, _latin(f"{config.PRODUCT_NAME} by {config.BRAND} - advice, not a guarantee of growth. Page {self.page_no()}"),
                  align="C")


def render_pdf(job: dict, audit: dict, path: Path) -> Path:
    m, subs, nar = audit["metrics"], audit["subscores"], audit["narrative"]
    pdf = _PDF(format="Letter")
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    W = pdf.w - 2 * pdf.l_margin

    def h(text, size=15):
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", size)
        pdf.set_text_color(20)
        pdf.multi_cell(W, 8, _latin(text))
        pdf.ln(1)

    def p(text, size=10.5, bold=False, color=40):
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.set_text_color(color)
        pdf.multi_cell(W, 5.6, _latin(text))
        pdf.ln(1.5)

    pdf.set_font("Helvetica", "B", 22)
    pdf.multi_cell(W, 10, _latin(f"{config.PRODUCT_NAME}: {m['channel'].get('title') or 'Your channel'}"))
    p(f"{job['tier'].title()} audit - {m['channel']['videos_analyzed']} videos analysed", 10, color=110)

    pdf.set_font("Helvetica", "B", 44)
    pdf.set_text_color(30, 90, 200)
    pdf.cell(40, 20, str(audit["score"]))
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(90)
    pdf.cell(0, 20, "/ 100 channel score", new_x="LMARGIN", new_y="NEXT")
    for k, label in SUB_LABELS.items():
        v = subs.get(k)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(60)
        pdf.cell(32, 6, label)
        pdf.set_fill_color(230, 233, 240)
        x, y = pdf.get_x(), pdf.get_y() + 1.5
        pdf.rect(x, y, 100, 3, "F")
        if v is not None:
            pdf.set_fill_color(30, 90, 200)
            pdf.rect(x, y, v, 3, "F")
        pdf.set_x(x + 104)
        pdf.cell(0, 6, "n/a (needs Studio data)" if v is None else str(v), new_x="LMARGIN", new_y="NEXT")

    h("Summary")
    p(nar.get("summary", ""))

    h("Top problems and fixes")
    for i, pr in enumerate(nar.get("problems", []), 1):
        p(f"{i}. {pr['title']}", 11.5, bold=True, color=20)
        p(pr["evidence"], color=70)
        p("Fix: " + pr["fix"])

    if nar.get("best_videos"):
        h("Your best-performing videos")
        p(nar["best_videos"])
        for v in m["outliers"]["top"]:
            p(f"- {v['title']} - {fmt(v['views'])} views ({v['multiple']}x median)", 9.5, color=80)
    if nar.get("format_breakdown"):
        h("Shorts vs long-form")
        p(nar["format_breakdown"])

    if job["tier"] == "premium":
        h("Title and thumbnail rewrites")
        titles = {v["video_id"]: v["title"] for v in m.get("recent_uploads", [])}
        for r in nar.get("rewrites", []):
            p(f"Was: {titles.get(r['video_id'], r['video_id'])}", 9.5, color=110)
            p(f"Now: {r['new_title']}", 11, bold=True, color=20)
            p(f"Thumbnail: {r['thumbnail_idea']}  ({r['why']})", 9.5)
        h("Your 30-day plan")
        p(nar.get("plan_intro", ""))
        for s in nar.get("schedule", []):
            p(f"Day {s['day']} - {s['format']}: {s['idea']}", 9.5)

    h("Key numbers", 13)
    c, mo, fm = m["cadence"], m["momentum"], m["format_mix"]
    rows = [("Uploads per week (last 90 days)", c["uploads_per_week"]), ("Longest gap (days)", c["longest_gap_days"]),
            ("Median views, last 10 uploads", mo["recent_median_views"]), ("Median views, 10 before", mo["previous_median_views"]),
            ("Shorts share of uploads (%)", fm["shorts_share_pct"])]
    if "packaging" in m:
        rows += [("Average click-through rate (%)", m["packaging"]["avg_ctr_pct"]),
                 ("Long-form avg % viewed", m["retention"]["longform_avg_pct_viewed"]),
                 ("Shorts avg % viewed", m["retention"]["shorts_avg_pct_viewed"]),
                 ("Subscribers per 1,000 views", m["conversion"]["subs_per_1k_views"])]
    for label, v in rows:
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(70)
        pdf.cell(110, 6, _latin(label))
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, fmt(v), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    p("This audit is advice based on your data, not a guarantee of growth. "
      f"To delete your data, visit {config.BASE_URL}/delete-my-data or email {config.SUPPORT_EMAIL}.", 8.5, color=120)
    pdf.output(str(path))
    return path
