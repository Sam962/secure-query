SELECT "Invoice"."BillingCountry", COUNT(*) AS "invoice_count" FROM "Invoice" GROUP BY "Invoice"."BillingCountry" ORDER BY "invoice_count" DESC, "Invoice"."BillingCountry" ASC LIMIT 10
