# SQLite identifier audit: full detail

Extracted from `artifacts/sqlite_identifier_audit.jsonl` (gitignored,
mechanically reproducible by running `pipeline/00_audit_sqlite_identifiers.py`).
That file has one JSON object per retained DB with every risky identifier
listed individually; this file pulls out the categorized, complete lists that
`audit-findings.md` only summarizes with counts and a handful of
illustrative examples. See `audit-findings.md` for the narrative (why the
audit exists, what it resolved, per-DB counts). This file is the raw
enumeration to consult when you need to know exactly which identifier, in
which table, in which DB, not just how many.

Of the 2,351 total risky identifiers found across 69 retained DBs:

| Category | Count |
| --- | --- |
| Embedded spaces | 106 |
| Punctuation (non-space) | 29 |
| Hyphenated table names (subset of punctuation, listed separately since they aren't valid unquoted SQL identifiers *at all*, not just non-lowercase) | 4 |
| Case-only (PascalCase/camelCase/UPPERCASE, no space or punctuation) | 2,216 |

The case-only 2,216 aren't enumerated here. See `audit-findings.md`'s
per-database table for those counts; the identifiers themselves are exactly
what `PRAGMA table_info()` already reports, nothing to add. The categories
below are the ones worth having on hand verbatim, since spaces/punctuation
are what actually break naive unquoted SQL (not just casing).

## Embedded spaces (106)

Format: `db.table[.column] -> "identifier"` (table-name entries have no
`.column` segment).

```text
airline.Air Carriers  ->  "Air Carriers"
app_store.playstore.Content Rating  ->  "Content Rating"
california_schools.frpm.Academic Year  ->  "Academic Year"
california_schools.frpm.County Code  ->  "County Code"
california_schools.frpm.District Code  ->  "District Code"
california_schools.frpm.School Code  ->  "School Code"
california_schools.frpm.County Name  ->  "County Name"
california_schools.frpm.District Name  ->  "District Name"
california_schools.frpm.School Name  ->  "School Name"
california_schools.frpm.District Type  ->  "District Type"
california_schools.frpm.School Type  ->  "School Type"
california_schools.frpm.Educational Option Type  ->  "Educational Option Type"
california_schools.frpm.NSLP Provision Status  ->  "NSLP Provision Status"
california_schools.frpm.Charter School (Y/N)  ->  "Charter School (Y/N)"
california_schools.frpm.Charter School Number  ->  "Charter School Number"
california_schools.frpm.Charter Funding Type  ->  "Charter Funding Type"
california_schools.frpm.Low Grade  ->  "Low Grade"
california_schools.frpm.High Grade  ->  "High Grade"
california_schools.frpm.Enrollment (K-12)  ->  "Enrollment (K-12)"
california_schools.frpm.Free Meal Count (K-12)  ->  "Free Meal Count (K-12)"
california_schools.frpm.Percent (%) Eligible Free (K-12)  ->  "Percent (%) Eligible Free (K-12)"
california_schools.frpm.FRPM Count (K-12)  ->  "FRPM Count (K-12)"
california_schools.frpm.Percent (%) Eligible FRPM (K-12)  ->  "Percent (%) Eligible FRPM (K-12)"
california_schools.frpm.Enrollment (Ages 5-17)  ->  "Enrollment (Ages 5-17)"
california_schools.frpm.Free Meal Count (Ages 5-17)  ->  "Free Meal Count (Ages 5-17)"
california_schools.frpm.Percent (%) Eligible Free (Ages 5-17)  ->  "Percent (%) Eligible Free (Ages 5-17)"
california_schools.frpm.FRPM Count (Ages 5-17)  ->  "FRPM Count (Ages 5-17)"
california_schools.frpm.Percent (%) Eligible FRPM (Ages 5-17)  ->  "Percent (%) Eligible FRPM (Ages 5-17)"
california_schools.frpm.2013-14 CALPADS Fall 1 Certification Status  ->  "2013-14 CALPADS Fall 1 Certification Status"
disney.revenue.Studio Entertainment[NI 1]  ->  "Studio Entertainment[NI 1]"
disney.revenue.Disney Consumer Products[NI 2]  ->  "Disney Consumer Products[NI 2]"
disney.revenue.Disney Interactive[NI 3][Rev 1]  ->  "Disney Interactive[NI 3][Rev 1]"
disney.revenue.Walt Disney Parks and Resorts  ->  "Walt Disney Parks and Resorts"
disney.revenue.Disney Media Networks  ->  "Disney Media Networks"
regional_sales.Customers.Customer Names  ->  "Customer Names"
regional_sales.Products.Product Name  ->  "Product Name"
regional_sales.Sales Team  ->  "Sales Team"
regional_sales.Sales Team.Sales Team  ->  "Sales Team"
regional_sales.Store Locations  ->  "Store Locations"
regional_sales.Store Locations.City Name  ->  "City Name"
regional_sales.Store Locations.Household Income  ->  "Household Income"
regional_sales.Store Locations.Median Income  ->  "Median Income"
regional_sales.Store Locations.Land Area  ->  "Land Area"
regional_sales.Store Locations.Water Area  ->  "Water Area"
regional_sales.Store Locations.Time Zone  ->  "Time Zone"
regional_sales.Sales Orders  ->  "Sales Orders"
regional_sales.Sales Orders.Sales Channel  ->  "Sales Channel"
regional_sales.Sales Orders.Order Quantity  ->  "Order Quantity"
regional_sales.Sales Orders.Discount Applied  ->  "Discount Applied"
regional_sales.Sales Orders.Unit Price  ->  "Unit Price"
regional_sales.Sales Orders.Unit Cost  ->  "Unit Cost"
retail_complains.callcenterlogs.Date received  ->  "Date received"
retail_complains.callcenterlogs.Complaint ID  ->  "Complaint ID"
retail_complains.callcenterlogs.rand client  ->  "rand client"
retail_complains.events.Date received  ->  "Date received"
retail_complains.events.Consumer complaint narrative  ->  "Consumer complaint narrative"
retail_complains.events.Consumer consent provided?  ->  "Consumer consent provided?"
retail_complains.events.Submitted via  ->  "Submitted via"
retail_complains.events.Date sent to company  ->  "Date sent to company"
retail_complains.events.Company response to consumer  ->  "Company response to consumer"
retail_complains.events.Timely response?  ->  "Timely response?"
retail_complains.events.Consumer disputed?  ->  "Consumer disputed?"
retail_complains.events.Complaint ID  ->  "Complaint ID"
superstore.people.Customer ID  ->  "Customer ID"
superstore.people.Customer Name  ->  "Customer Name"
superstore.people.Postal Code  ->  "Postal Code"
superstore.product.Product ID  ->  "Product ID"
superstore.product.Product Name  ->  "Product Name"
superstore.central_superstore.Row ID  ->  "Row ID"
superstore.central_superstore.Order ID  ->  "Order ID"
superstore.central_superstore.Order Date  ->  "Order Date"
superstore.central_superstore.Ship Date  ->  "Ship Date"
superstore.central_superstore.Ship Mode  ->  "Ship Mode"
superstore.central_superstore.Customer ID  ->  "Customer ID"
superstore.central_superstore.Product ID  ->  "Product ID"
superstore.east_superstore.Row ID  ->  "Row ID"
superstore.east_superstore.Order ID  ->  "Order ID"
superstore.east_superstore.Order Date  ->  "Order Date"
superstore.east_superstore.Ship Date  ->  "Ship Date"
superstore.east_superstore.Ship Mode  ->  "Ship Mode"
superstore.east_superstore.Customer ID  ->  "Customer ID"
superstore.east_superstore.Product ID  ->  "Product ID"
superstore.south_superstore.Row ID  ->  "Row ID"
superstore.south_superstore.Order ID  ->  "Order ID"
superstore.south_superstore.Order Date  ->  "Order Date"
superstore.south_superstore.Ship Date  ->  "Ship Date"
superstore.south_superstore.Ship Mode  ->  "Ship Mode"
superstore.south_superstore.Customer ID  ->  "Customer ID"
superstore.south_superstore.Product ID  ->  "Product ID"
superstore.west_superstore.Row ID  ->  "Row ID"
superstore.west_superstore.Order ID  ->  "Order ID"
superstore.west_superstore.Order Date  ->  "Order Date"
superstore.west_superstore.Ship Date  ->  "Ship Date"
superstore.west_superstore.Ship Mode  ->  "Ship Mode"
superstore.west_superstore.Customer ID  ->  "Customer ID"
superstore.west_superstore.Product ID  ->  "Product ID"
synthea.all_prevalences.POPULATION TYPE  ->  "POPULATION TYPE"
synthea.all_prevalences.POPULATION COUNT  ->  "POPULATION COUNT"
synthea.all_prevalences.PREVALENCE RATE  ->  "PREVALENCE RATE"
synthea.all_prevalences.PREVALENCE PERCENTAGE  ->  "PREVALENCE PERCENTAGE"
thrombosis_prediction.Examination.Examination Date  ->  "Examination Date"
thrombosis_prediction.Examination.aCL IgG  ->  "aCL IgG"
thrombosis_prediction.Examination.aCL IgM  ->  "aCL IgM"
thrombosis_prediction.Examination.ANA Pattern  ->  "ANA Pattern"
thrombosis_prediction.Examination.aCL IgA  ->  "aCL IgA"
thrombosis_prediction.Patient.First Date  ->  "First Date"
```

## Punctuation, non-space (29)

Includes the two `disney.voice-actors` entries. The table name itself is
hyphenated (also listed in the hyphenated-tables section below since that's
the more severe case) and its `voice-actor` column happens to be hyphenated
too, independently.

```text
disney.voice-actors  ->  "voice-actors"
disney.voice-actors.voice-actor  ->  "voice-actor"
hockey.CombinedShutouts.R/P  ->  "R/P"
hockey.Goalies.T/OL  ->  "T/OL"
hockey.Scoring.+/-  ->  "+/-"
hockey.Scoring.Post+/-  ->  "Post+/-"
legislator.current-terms  ->  "current-terms"
legislator.historical-terms  ->  "historical-terms"
legislator.social-media  ->  "social-media"
retail_complains.callcenterlogs.vru+line  ->  "vru+line"
retail_complains.events.Sub-product  ->  "Sub-product"
retail_complains.events.Sub-issue  ->  "Sub-issue"
superstore.product.Sub-Category  ->  "Sub-Category"
talkingdata.sample_submission.F23-  ->  "F23-"
talkingdata.sample_submission.F24-26  ->  "F24-26"
talkingdata.sample_submission.F27-28  ->  "F27-28"
talkingdata.sample_submission.F29-32  ->  "F29-32"
talkingdata.sample_submission.F33-42  ->  "F33-42"
talkingdata.sample_submission.F43+  ->  "F43+"
talkingdata.sample_submission.M22-  ->  "M22-"
talkingdata.sample_submission.M23-26  ->  "M23-26"
talkingdata.sample_submission.M27-28  ->  "M27-28"
talkingdata.sample_submission.M29-31  ->  "M29-31"
talkingdata.sample_submission.M32-38  ->  "M32-38"
talkingdata.sample_submission.M39+  ->  "M39+"
thrombosis_prediction.Laboratory.T-BIL  ->  "T-BIL"
thrombosis_prediction.Laboratory.T-CHO  ->  "T-CHO"
thrombosis_prediction.Laboratory.U-PRO  ->  "U-PRO"
thrombosis_prediction.Laboratory.DNA-II  ->  "DNA-II"
```

## Hyphenated table names (4)

Not valid unquoted SQL identifiers *at all*, independent of casing: a
hyphen inside an unquoted identifier is parsed as a subtraction operator, so
these can only ever work quoted. Confirmed (2026-07-01, live pgloader run)
that `quote identifiers` + `create no indexes` handles these correctly; no
special per-DB override was ultimately needed for either `legislator` or
`disney` despite `audit-findings.md`'s "suggested fixes" §3 originally
flagging this as an open risk requiring verification.

```text
disney."voice-actors"
legislator."current-terms"
legislator."historical-terms"
legislator."social-media"
```
