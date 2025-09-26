import os


def ORG_INFO():
    return {
        "name": os.getenv("ORG_NAME", "MU AI Center"),
        "address_line1": os.getenv("ORG_ADDR1", "999 Phutthamonthon Sai 4 Road"),
        "address_line2": os.getenv("ORG_ADDR2", "Salaya"),
        "city": os.getenv("ORG_CITY", "Nakhon Pathom"),
        "postcode": os.getenv("ORG_POST", "73170"),
        "country": os.getenv("ORG_COUNTRY", "Thailand"),
        "tax_id": os.getenv("ORG_TAX_ID", "0994000158378"),
        "email": os.getenv("ORG_EMAIL", "opwww@mahidol.ac.th"),
        "phone": os.getenv("ORG_PHONE", "+66 (0) 2849-6000"),
        "bank_name": os.getenv("ORG_BANK_NAME", "Siam Commercial Bank"),
        "bank_acct": os.getenv("ORG_BANK_ACCT", "123-4-56789-0"),
        "bank_holder": os.getenv("ORG_BANK_HOLDER", "Mahidol University"),
    }


def ORG_INFO_TH():
    return {
        "name": os.getenv("ORG_NAME", "สถาบันปัญญาประดิษฐ์มหิดล"),
        "address_line1": os.getenv("ORG_ADDR1", " 999 ถ.พุทธมณฑลสาย 4"),
        "address_line2": os.getenv("ORG_ADDR2", "ต.ศาลายา อ.พุทธมณฑล"),
        "city": os.getenv("ORG_CITY", "จังหวัดนครปฐม"),
        "postcode": os.getenv("ORG_POST", "73170"),
        "country": os.getenv("ORG_COUNTRY", "ประเทศไทย"),
        "tax_id": os.getenv("ORG_TAX_ID", "0994000158378"),
        "email": os.getenv("ORG_EMAIL", "opwww@mahidol.ac.th"),
        "phone": os.getenv("ORG_PHONE", "+66 (0) 2849-6000"),
        "bank_name": os.getenv("ORG_BANK_NAME", "ธนาคารไทยพาณิชย์"),
        "bank_acct": os.getenv("ORG_BANK_ACCT", "123-4-56789-0"),
        "bank_holder": os.getenv("ORG_BANK_HOLDER", "มหาวิทยาลัยมหิดล"),
    }
