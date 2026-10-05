from tasks import generate_invoice

for account in ("acme", "globex", "closed-initech"):
    generate_invoice.delay(account, "2026-10")
print("queued 3 invoices")
