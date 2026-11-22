"""PDF for paid website buyers: listing check-up + 30-day plan + how to use the new site (fpdf2)."""
from __future__ import annotations

from fpdf import FPDF

from . import config
from .report import _latin


class _PDF(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", size=8)
        self.set_text_color(130)
        self.cell(0, 6, _latin(f"{config.BRAND} Websites - advice, not a guarantee of rankings or sales. Page {self.page_no()}"),
                  align="C")


def render(name: str, address: str, score: int | None, insights: list[dict], plan: list[dict],
           site_url: str | None, pro: bool) -> bytes:
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
    pdf.multi_cell(W, 10, _latin(f"{name}: online check-up and 30-day plan"))
    if address:
        p(address, 10, color=110)
    if score is not None:
        pdf.set_font("Helvetica", "B", 44)
        pdf.set_text_color(124, 58, 237)
        pdf.cell(40, 20, str(score))
        pdf.set_font("Helvetica", "", 12)
        pdf.set_text_color(90)
        pdf.cell(0, 20, "/ 100 Google listing score (before your new website)", new_x="LMARGIN", new_y="NEXT")

    if site_url:
        h("Your website")
        p(site_url, 11.5, bold=True, color=20)
        p("Put this link as the 'Website' on your Google Business Profile, your social profiles and your receipts. "
          + ("We're polishing the copy and photos and connecting your domain; you'll get an email when it's done."
             if pro else "Need a change to text, services or hours? Reply to your confirmation email."))

    if insights:
        h("Your listing check-up")
        for i in insights:
            p(("OK  " if i["passed"] else "FIX  ") + i["label"], 11, bold=True, color=20 if i["passed"] else 150)
            p(i["finding"], color=70)
            if i.get("fix"):
                p("Fix: " + i["fix"].replace(" (We built you one below.)", " Your new site is above: use that link."))

    h("Your 30-day plan")
    p("About 15 minutes a day. Tick each one off; the week-4 check-in shows what changed.", color=90)
    for wk in plan:
        p(wk["title"], 11.5, bold=True, color=20)
        for t in wk["tasks"]:
            p("[ ]  " + t, 10)

    pdf.ln(4)
    p("Better listings tend to earn more calls, clicks and direction requests, but nobody can promise a ranking. "
      f"Questions: {config.SUPPORT_EMAIL}.", 8.5, color=120)
    return bytes(pdf.output())
