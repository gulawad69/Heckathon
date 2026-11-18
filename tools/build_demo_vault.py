"""Writes the DEMO knowledge vault (atomic Obsidian notes, finaldesign.md §5). SYNTHETIC DEMO CONTENT:
must be replaced with the tenant's pharmacist-approved formulary/SOPs before any real use. Not clinical advice."""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "knowledge_vault")
BANNER = "\n\n> DEMO CONTENT - tenant-approved monographs must replace this note. Not a substitute for pharmacist advice."

A = {
 "FORM-PARACETAMOL": ("formulary", 1, "Paracetamol (acetaminophen) 500 mg tablets",
  "Paracetamol is a listed over-the-counter option for mild to moderate pain such as headache, toothache or muscle ache, and for fever in adults. "
  "Usual adult dose is 500 mg to 1000 mg every 4 to 6 hours. The formulary adult maximum is 4 g in 24 hours, and 3 g in 24 hours for adults with liver disease, "
  "regular heavy alcohol use or low body weight. Check other products for hidden paracetamol before combining. See [[SAFETY-PREGNANCY]], [[SAFETY-PEDIATRIC]], [[POLICY-ESCALATION]]."),
 "FORM-IBUPROFEN": ("formulary", 1, "Ibuprofen 200 mg tablets",
  "Ibuprofen is a listed over-the-counter NSAID for mild to moderate pain with inflammation, such as headache, dental pain, period pain or muscle ache, and for fever in adults. "
  "Usual adult dose is 200 mg to 400 mg every 6 to 8 hours with food, up to 1200 mg in 24 hours for self-treatment. "
  "Do not use with a history of stomach ulcer or bleeding, severe kidney impairment, or asthma triggered by NSAIDs. Avoid in the third trimester of pregnancy. See [[SAFETY-NSAID-INTERACTIONS]], [[SAFETY-PREGNANCY]], [[POLICY-ESCALATION]]."),
 "FORM-CETIRIZINE": ("formulary", 1, "Cetirizine 10 mg tablets",
  "Cetirizine is a listed over-the-counter second-generation antihistamine for hay fever, allergic rhinitis and mild allergic skin itching in adults. "
  "Usual adult dose is 10 mg once daily. It may cause drowsiness in some people, so caution is advised when driving. See [[POLICY-ESCALATION]], [[SAFETY-PEDIATRIC]]."),
 "FORM-LORATADINE": ("formulary", 1, "Loratadine 10 mg tablets",
  "Loratadine is a listed over-the-counter non-drowsy antihistamine for hay fever and allergic rhinitis symptoms such as sneezing and runny nose in adults. "
  "Usual adult dose is 10 mg once daily. See [[POLICY-ESCALATION]]."),
 "FORM-ORS": ("formulary", 1, "Oral rehydration salts (ORS) sachets",
  "Oral rehydration salts are a listed over-the-counter option to replace fluid and electrolytes in mild diarrhoea or vomiting. Dissolve one sachet in the volume of clean water stated on the pack and sip frequently. "
  "Seek pharmacist or medical advice if diarrhoea lasts more than 2 days, if there is blood in the stool, or for infants and the elderly. See [[SAFETY-PEDIATRIC]], [[POLICY-ESCALATION]]."),
 "FORM-OMEPRAZOLE": ("formulary", 1, "Omeprazole 20 mg gastro-resistant capsules (OTC)",
  "Omeprazole 20 mg is a listed over-the-counter option for frequent heartburn in adults, taken once daily for up to 14 days. "
  "Seek medical advice if symptoms persist beyond 14 days, or if there is difficulty swallowing, black stools, or unexplained weight loss. See [[POLICY-ESCALATION]]."),
 "SAFETY-NSAID-INTERACTIONS": ("safety", 2, "NSAID combination safety",
  "Do not combine ibuprofen with other NSAIDs such as aspirin for pain or naproxen, because the risk of stomach bleeding and kidney harm increases. "
  "People taking anticoagulants, such as warfarin, or on regular aspirin must ask a pharmacist before using ibuprofen. See [[FORM-IBUPROFEN]]."),
 "SAFETY-PEDIATRIC": ("safety", 2, "Children: pharmacist-led dosing",
  "This assistant does not calculate or suggest medicine doses for children. Pediatric doses depend on age and weight and must be confirmed by a pharmacist or prescriber. "
  "Refer to the pharmacist on duty. See [[POLICY-ESCALATION]]."),
 "SAFETY-PREGNANCY": ("safety", 2, "Pregnancy and breastfeeding",
  "Anyone who is pregnant or breastfeeding must speak to a pharmacist or prescriber before taking any medicine. Paracetamol at the lowest effective dose is commonly considered first choice for pain or fever, "
  "while ibuprofen and other NSAIDs are avoided in the third trimester. See [[FORM-PARACETAMOL]], [[FORM-IBUPROFEN]]."),
 "POLICY-ESCALATION": ("policy", 3, "When to escalate to a pharmacist or emergency care",
  "Refer to the pharmacist on duty if symptoms last longer than the label allows, if the person takes other regular medicines, or has kidney, liver, heart or stomach conditions. "
  "For chest pain, difficulty breathing, swelling of the face or throat, suspected overdose or thoughts of self-harm, direct the person to emergency services immediately. See [[POLICY-RX-ONLY]]."),
 "POLICY-RX-ONLY": ("policy", 3, "Prescription-only medicines",
  "The assistant lists only formulary over-the-counter products. It never recommends, starts, changes or dispenses prescription-only medicines. These require a licensed prescriber and a pharmacist. See [[POLICY-ESCALATION]]."),
 "SOP-LOT-QUARANTINE": ("sop", 3, "Batch lot state transitions",
  "Changing the state of a medicine batch lot, for example from AVAILABLE to QUARANTINED, RELEASED or RECALLED, is a TIER_3 action. "
  "The assistant can only submit a request, which stays PENDING_APPROVAL until a pharmacist approver in the same tenant approves it. See [[SOP-STORAGE]]."),
 "SOP-STORAGE": ("sop", 2, "Storage of over-the-counter tablets",
  "Store tablets in the original pack, below 25 degrees Celsius, in a dry place away from direct light and out of reach of children. Do not use after the expiry date on the pack. See [[SOP-LOT-QUARANTINE]]."),
}
B = {
 "FORM-PARACETAMOL": ("formulary", 1, "Paracetamol 500 mg tablets (site B policy)",
  "At site B paracetamol is the listed over-the-counter option for mild pain and fever in adults. Site B policy sets the adult maximum at 3 g in 24 hours. See [[POLICY-ESCALATION]]."),
 "FORM-ASPIRIN-B": ("formulary", 1, "Aspirin 300 mg dispersible tablets (site B only)",
  "At site B, soluble aspirin 300 mg is listed for short-term relief of headache in adults, every 4 to 6 hours with a maximum of 3 g in 24 hours. Never give to children under 16. See [[POLICY-ESCALATION]]."),
 "POLICY-ESCALATION": ("policy", 3, "Site B escalation",
  "At site B refer to the pharmacist for any symptom lasting more than 3 days, and send emergency symptoms to emergency services immediately. See [[POLICY-RX-ONLY]]."),
 "POLICY-RX-ONLY": ("policy", 3, "Site B prescription-only medicines",
  "Site B assistant lists only over-the-counter formulary products and never recommends prescription-only medicines. See [[POLICY-ESCALATION]]."),
}
for tenant, notes in (("tenant_pharma_a", A), ("tenant_pharma_b", B)):
    d = root / tenant
    d.mkdir(parents=True, exist_ok=True)
    for nid, (domain, tier, title, body) in notes.items():
        (d / f"{nid}.md").write_text(
            f"---\nid: {nid}\ndomain: {domain}\nrisk_tier: {tier}\nversion: 1.0.0\ntitle: {title}\n---\n# {title}\n\n{body}{BANNER}\n")
print("wrote", sum(1 for _ in root.rglob("*.md")), "notes")
