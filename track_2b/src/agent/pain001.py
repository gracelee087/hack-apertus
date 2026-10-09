"""ISO 20022 pain.001.001.09 credit-transfer file per client (one file the client's bank imports)."""
import xml.etree.ElementTree as ET
from datetime import datetime

from .common import iban_valid, normalize_iban

NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, f"{{{NS}}}{tag}")
    if text is not None:
        el.text = str(text)
    return el


def build(mandate: dict, payments: list, msg_id: str, created: datetime, exec_date: str) -> str:
    """payments: [{end_to_end_id, creditor, iban, amount_chf, reference}]"""
    ET.register_namespace("", NS)
    total = f"{sum(round(p['amount_chf'], 2) for p in payments):.2f}"
    doc = ET.Element(f"{{{NS}}}Document")
    root = _sub(doc, "CstmrCdtTrfInitn")
    hdr = _sub(root, "GrpHdr")
    _sub(hdr, "MsgId", msg_id)
    _sub(hdr, "CreDtTm", created.strftime("%Y-%m-%dT%H:%M:%S"))
    _sub(hdr, "NbOfTxs", len(payments))
    _sub(hdr, "CtrlSum", total)
    _sub(_sub(hdr, "InitgPty"), "Nm", mandate["name"])
    pmt = _sub(root, "PmtInf")
    _sub(pmt, "PmtInfId", f"{msg_id}-1")
    _sub(pmt, "PmtMtd", "TRF")
    _sub(pmt, "NbOfTxs", len(payments))
    _sub(pmt, "CtrlSum", total)
    _sub(_sub(pmt, "ReqdExctnDt"), "Dt", exec_date)
    _sub(_sub(pmt, "Dbtr"), "Nm", mandate["name"])
    _sub(_sub(_sub(pmt, "DbtrAcct"), "Id"), "IBAN", mandate["debtor_iban"])
    _sub(_sub(pmt, "DbtrAgt"), "FinInstnId")
    for p in payments:
        tx = _sub(pmt, "CdtTrfTxInf")
        _sub(_sub(tx, "PmtId"), "EndToEndId", p["end_to_end_id"])
        amt = _sub(_sub(tx, "Amt"), "InstdAmt", f"{p['amount_chf']:.2f}")
        amt.set("Ccy", "CHF")
        _sub(_sub(tx, "Cdtr"), "Nm", p["creditor"][:70])
        _sub(_sub(_sub(tx, "CdtrAcct"), "Id"), "IBAN", normalize_iban(p["iban"]))
        _sub(_sub(tx, "RmtInf"), "Ustrd", str(p.get("reference", ""))[:140])
    ET.indent(doc)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding="unicode")


def validate(xml_text: str) -> list:
    """Structural checks (no XSD available offline): counts, control sums, IBANs, amounts."""
    errors = []
    q = lambda path: path.replace("/", f"/{{{NS}}}").lstrip("/")
    root = ET.fromstring(xml_text)
    init = root.find(q("/CstmrCdtTrfInitn"))
    if root.tag != f"{{{NS}}}Document" or init is None:
        return ["not a pain.001.001.09 document"]
    txs = init.findall(q("/PmtInf/CdtTrfTxInf"))
    amounts = [float(t.find(q("/Amt/InstdAmt")).text) for t in txs]
    hdr_n = int(init.find(q("/GrpHdr/NbOfTxs")).text)
    hdr_sum = float(init.find(q("/GrpHdr/CtrlSum")).text)
    if hdr_n != len(txs):
        errors.append(f"NbOfTxs {hdr_n} != {len(txs)} transactions")
    if abs(hdr_sum - sum(amounts)) > 0.005:
        errors.append(f"CtrlSum {hdr_sum} != {sum(amounts):.2f}")
    if any(a <= 0 for a in amounts):
        errors.append("non-positive amount")
    for el in init.iter(f"{{{NS}}}IBAN"):
        if not iban_valid(el.text):
            errors.append(f"invalid IBAN {el.text}")
    ids = [t.find(q("/PmtId/EndToEndId")).text for t in txs]
    if len(set(ids)) != len(ids):
        errors.append("duplicate EndToEndId")
    return errors
