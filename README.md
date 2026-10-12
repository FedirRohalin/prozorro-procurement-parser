# Prozorro Procurement Parser & Data Enricher

Automated ETL pipeline for parsing Ukraine's public procurement data (Prozorro API), targeting military UAV/drone suppliers, and enriching B2B contact info via Opendatabot / YouControl APIs.

## Features
- Prozorro REST API parsing with pagination & rate limiting
- CPV code & keyword filtering (`35610000-4`, БпАК, дрон, etc.)
- Supplier identification & EDRPOU extraction
- Contact data enrichment
- Aggregation and deduplication into JSON/CSV