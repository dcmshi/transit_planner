# GO Transit API — License Restrictions

> Source: Metrolinx GO Transit API Access and Use Agreement
> Saved: 2026-02-10
> Review this file before any changes to data handling, branding, or distribution.

---

## Quick Reference — What We Must NOT Do

| # | Restriction | Relevant to this project |
|---|-------------|--------------------------|
| 1 | **No redistribution** — do not expose raw feed data through our own API or feed | Our `/routes` endpoint must return derived results, not raw GTFS/RT data |
| 2 | **No resale or transfer** — cannot license, sell, rent or assign the Data | N/A (personal/research use only) |
| 3 | **No GO Transit branding** — no trademarks, logos, or GO/UP Express marks in the app | Do not use GO logos, colours, or names in any UI |
| 4 | **No "official app" implication** — must not suggest this is an official GO Transit product | Add a clear disclaimer in any UI or docs |
| 5 | **No press releases** mentioning GO Transit without prior written consent from Metrolinx | Don't publish blog posts / announcements naming GO Transit without approval |
| 6 | **Respect rate limits** — do not attempt to circumvent any API quotas Metrolinx imposes | Honour `GTFS_RT_POLL_SECONDS`; do not hammer the feed |
| 7 | **No scraping** of the GO Transit platform beyond the authorised API data | Only use the GTFS and GTFS-RT feeds we're registered for |
| 8 | **Do not replicate GO Transit's product** — app must not mirror the overall experience or visual design of the GO Transit website or app | Our tool is a reliability scorer, not a trip planner UI clone |

---

## What We CAN Do

- Access, use and download Data for the registered intended use (reliability routing research)
- Build derived products (risk scores, route explanations) from the Data
- Combine Data with other information (e.g. walking distances, LLM explanations)
- Acknowledge that the service uses GO Transit data without using their trademarks

---

## Key Obligations

- **Data is "as is"** — Metrolinx makes no accuracy guarantees; our app must not present the data as authoritative or guaranteed
- **Metrolinx owns all data** — including any improvements or modifications derived from it
- **Indemnification** — we are responsible for any third-party claims arising from our use of the Data
- **Ontario law governs** — agreement is subject to the laws of the Province of Ontario
- **Agreement can change** — Metrolinx can update terms without notice; continued use = acceptance
- **Access can be revoked** — Metrolinx can cancel access at any time without notice

---

## Implications for This Codebase

1. **`GET /routes` response** — returns scored/explained routes, not raw GTFS records. ✓ Compliant.
2. **`POST /ingest/gtfs-static`** — stores data locally in SQLite for routing purposes only, not re-served externally. ✓ Compliant.
3. **`GET /stops`** — exposes stop names and IDs to support the routing UI. This is derived use of the Data. Keep it internal; do not build a public stop search API on top of this.
4. **No raw feed proxy** — do not add any endpoint that passes through or republishes raw GTFS or GTFS-RT bytes.
5. **Disclaimer** — any user-facing UI must state clearly: *"This is not an official GO Transit product. Route information is provided for informational purposes only."*

---

## Full Agreement Text

### 1. Acceptance
Use of the GO Transit API is governed by this Agreement. Accessing, using, or downloading Data constitutes acceptance without modification.

### 2. License
Non-exclusive, limited, revocable licence to access, use and download Data. Metrolinx has no obligation to provide updates or additional Data.

### 3. Ownership
Metrolinx retains all rights, title and interest in the Data and all intellectual property therein. No proprietary rights are acquired by use. Metrolinx trademarks (GO, GO Transit, UP Express) may not be used in association with the Data.

### 4. Branding
No Metrolinx or GO Transit branding, trademarks or copyrighted works in advertising or promotional materials. Must not use the Metrolinx or GO Transit name or imply the app is official.

### 5. Press and Publicity
No press releases or public announcements referencing GO Transit without prior written consent from Metrolinx.

### 6. Usage and Quotas
Metrolinx may impose call frequency limits at its discretion. Circumvention of limits is prohibited.

### 7. General Prohibitions
- (a) Must not redistribute Data within your own API or feed.
- (b) Must not license, sell, lease, rent, lend, convey or transfer the Data.
- (c) Must not scrape the GO Transit platform beyond legitimately accessible API data.
- (d) Must not replicate a substantial number of GO Transit features or copy the visual design of the GO Transit platform.

### 8. Modification of the API
Metrolinx may modify the API at any time. Backwards compatibility is attempted but not guaranteed.

### 9. Disclaimer
Data provided "as is" and "as available". May not be complete or accurate. Use at your own risk. All warranties disclaimed.

### 10. Limitation of Liability
Metrolinx is not liable for any direct, indirect, special, incidental, or consequential damages arising from use of the Data or API, even if advised of the possibility.

### 11. Indemnification
You agree to indemnify and hold harmless Metrolinx against any third-party claims arising from (i) your breach of this Agreement or (ii) your use of the Data or API.

### 12. Availability
Metrolinx will make reasonable efforts to keep the API available but does not guarantee uninterrupted or error-free access, or freedom from security vulnerabilities.

### 13. Cessation of Access
Metrolinx may alter or discontinue the API at any time without prior written notice.

### 14. Cancellation for Non-Compliance
Access may be suspended or cancelled without notice for unlawful use, harm to others, or any breach of this Agreement.

### 15. General
- (a) Governed by the laws of the Province of Ontario.
- (b) Metrolinx may update this Agreement at any time; continued use constitutes acceptance.
- (c) You must comply with all applicable laws where the Data is used.
- (d) Invalid provisions are severable; remaining terms continue in force.
- (e) This Agreement is the entire agreement between the parties on this subject.
- (f) No waiver of rights unless made in writing by Metrolinx.
