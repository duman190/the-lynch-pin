# 📌 The Lynch Pin: Automated PEG Deal Detector
> **Status:** Active (M1 Mac Server) | **Goal:** 5M X Impressions | **Engine:** Python + Gemini 2.5 Flash

`The Lynch Pin` is a high-frequency quantitative scanner that identifies "Growth at a Discount" across 6 major indices ($QQQ, $VOO, $SCHD, $IGV, $SMH, $VTI). It automates the entire pipeline from raw data scraping to AI-generated market sentiment and X-thread publishing.

---

## ⚡ Vibe Coding Protocol
*If you are an AI agent helping me update this code, adhere to these constraints:*

1. **Hardware Awareness:** Running on a MacBook Air M1 (8GB). Optimize for low memory. Use `yfinance` for data; avoid heavy headless browsers (Selenium/Playwright).
2. **The "Lynch" Logic:** - **Strategy:** Growth at a Reasonable Price (GARP).
   - **Deal Filter:** Current PEG must be $> 1.5$ Standard Deviations below the 5-year historical mean.
   - **ROI Calculation:** 5-year annualized projections based on mean reversion (Bull/Base/Bear).
3. **Tone & Brand:** Content must be professional, quantitative, and authoritative. Avoid "get rich quick" language.
4. **LLM Efficiency:** Use `gemini-2.5-flash` for narrative summaries. Keep prompt tokens low and respect the Free Tier rate limits.

---

## 🛠 Tech Stack & Automation
- **Compute:** 2023 MacBook Air M1 — 24/7 "Silent Server" configuration.
- **Automation:** `pmset` + `caffeinate` (Auto-wake at 14:05 PST for market close processing).
- **Core:** Python 3.12 (Pandas, Matplotlib, YFinance).
- **Intelligence:** Google Gemini API (Natural Language summary of macro risks).
- **Social:** X (Twitter) API v2 via `tweepy`.

---

## 📈 The Analysis Engine
The daily pipeline runs the following sequence:
1. **The Scan:** Pulls TTM PE and Forward Growth for 600+ tickers across 6 indices.
2. **The Z-Score Filter:** Ranks deals by `(Current PEG - 5yr Mean) / 5yr SD`.
3. **The ROI Funnel:**
   - **Bull:** PEG reverts to Mean + 0.5 SD.
   - **Base:** PEG reverts to 5-year Mean.
   - **Bear:** Zero multiple expansion (ROI driven by EPS growth only).
4. **The Synthesis:** Gemini AI reviews the Top 10 deals and identifies common sector themes or specific earnings-related risks.

---

## 📂 Project Structure
- `lynch_pin_core.py` - The main data processing and ROI engine.
- `database/peg_history.json` - Local cache of 5-year PEG means/SD for all tickers.
- `graphics/` - Automated Matplotlib exports (Heatmaps and Index Tables).
- `logs/` - Daily performance tracking and CSV archives of "Top Deals."

---

## ⚠️ Compliance & Disclaimer
- **X API:** Respect daily posting limits to avoid shadowbanning.
- **Financial Advice:** Every thread must include: *"Not financial advice. Automated quantitative scan by The Lynch Pin."*
- **Privacy:** Do not feed private brokerage data or PII into the LLM prompts.

---

### 💡 Upcoming Roadmap:
- [ ] Add R-Squared "Confidence Score" to ROI projections.
- [ ] Implement dark-mode Bloomberg-style Image Tables for X posts.
- [ ] Integrate with Seeking Alpha API for automated long-form drafting.
