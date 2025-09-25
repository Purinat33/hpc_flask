import os


def ORG_INFO():
    return {
        "name": os.getenv("ORG_NAME", "MU AI Center"),
        "address_line1": os.getenv("ORG_ADDR1", "999 Putthamonton"),
        "address_line2": os.getenv("ORG_ADDR2", ""),
        "city": os.getenv("ORG_CITY", "Nakhon Pathom"),
        "postcode": os.getenv("ORG_POST", "10110"),
        "country": os.getenv("ORG_COUNTRY", "Thailand"),
        "tax_id": os.getenv("ORG_TAX_ID", ""),
        "email": os.getenv("ORG_EMAIL", "billing@example.com"),
        "phone": os.getenv("ORG_PHONE", "+66 2 000 0000"),
        "bank_name": os.getenv("ORG_BANK_NAME", "Bangkok Bank"),
        "bank_acct": os.getenv("ORG_BANK_ACCT", "123-4-56789-0"),
        "bank_holder": os.getenv("ORG_BANK_HOLDER", "Mahidol University"),
    }
