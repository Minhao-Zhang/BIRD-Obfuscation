[English](identifier-audit-detail.md) · **中文**

# SQLite 标识符审计:完整明细

提取自 `artifacts/sqlite_identifier_audit.jsonl`(已被 gitignore,可通过运行
`pipeline/00_audit_sqlite_identifiers.py` 重新生成)。该文件为每个保留的数据库
生成一个 JSON 对象,并逐条列出所有存在风险的标识符;本文件则把 `audit-findings.md` 里只用计数和少量示例带过的内容,按类别完整列成清单。有关叙述性说明(审计存在的原因、它解决了什么问题、各数据库的计数),
请参见 `audit-findings.md`。当你需要确切知道是哪个标识符、位于哪张表、属于哪个
数据库,而不只是知道有多少个时,本文件就是可供查阅的原始枚举清单。

在 69 个保留数据库中共发现 2,351 个存在风险的标识符,其中:

| 类别 | 数量 |
| --- | --- |
| 含内嵌空格 | 106 |
| 标点符号(非空格) | 29 |
| 带连字符的表名(属于标点符号的子集,因其*根本*就不是合法的未加引号 SQL 标识符,而不只是非小写形式,故单独列出) | 4 |
| 仅大小写问题(PascalCase/camelCase/UPPERCASE,无空格或标点) | 2,216 |

仅大小写问题的这 2,216 个不在此处逐条列出。相关计数请参见 `audit-findings.md`
中的各数据库表格;这些标识符本身就是 `PRAGMA table_info()` 已经报告的内容,
无需额外补充。下面这些类别才值得原样保存备查,因为真正会破坏不加引号的朴素 SQL 的,是空格和标点,而不只是大小写。

## 含内嵌空格(106)

格式:`db.table[.column] -> "identifier"`(表名条目没有 `.column` 部分)。

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

## 标点符号,非空格(29)

包含两条 `disney.voice-actors` 条目。表名本身带连字符(它也出现在下方的“带连字符
的表名”小节中,因为那是更严重的情况),而它下面的 `voice-actor` 列碰巧自己也带连字符。

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

## 带连字符的表名(4)

无论大小写如何,它们*根本*就不是合法的未加引号 SQL 标识符:未加引号标识符中的
连字符会被解析为减法运算符,所以只有加了引号才能用。已确认
(2026-07-01,实际运行 pgloader)`quote identifiers` + `create no indexes` 能正确
处理它们;尽管 `audit-findings.md` 的“建议修复”(suggested fixes)第 3 节最初把
这一点标记为需要验证的未决风险,但 `legislator` 和 `disney` 最终都无需任何针对
特定数据库的特殊覆盖设置。

```text
disney."voice-actors"
legislator."current-terms"
legislator."historical-terms"
legislator."social-media"
```
