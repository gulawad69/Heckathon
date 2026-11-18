"""Triage (finaldesign.md §4.4): deterministic classifier (temp 0.0, <= 25 ms) -> (intent, domain, risk_tier, complexity)
+ route in {DIRECT_ANSWER, HYBRID_RAG, TRANSACTION_EXEC, REJECT_OOS}. Stand-in for the distilled encoder classifier
(§16.3): rule/lexicon based, same interface. risk_tier=3 always forces HITL regardless of route."""
from __future__ import annotations

import re
from dataclasses import dataclass

LOT_RX = re.compile(r"\blot[\s_\-]*\d{1,5}\b", re.I)
TX_VERB = re.compile(r"\b(quarantine|release|recall|hold)\b", re.I)
EMERGENCY = re.compile(r"\b(overdos\w*|chest pain|can'?t breathe|cannot breathe|difficulty breathing|anaphyla\w*|swelling of (?:the )?(?:face|throat)|"
                       r"suicid\w*|self[- ]harm|kill myself|seizure|unconscious|poison(?:ed|ing)?)\b", re.I)
CLINICAL_ACTION = re.compile(r"\b(prescribe|dispense|write (?:me )?a prescription|start (?:the )?patient on|put (?:the )?patient on|"
                             r"order (?:a )?(?:refill|medication|drug)|increase (?:the )?dose for (?:the )?patient|authori[sz]e (?:a )?refill|"
                             r"diagnose|give (?:my |the )?(?:child|baby|infant|toddler) )\b", re.I)
GREETING = re.compile(r"^\s*(hi|hello|hey|good (?:morning|afternoon|evening)|thanks|thank you|help|what can you do)\b[\s!.?]*$", re.I)
DOMAIN = re.compile(r"\b(medicine|medication|medicines|drug|tablet|tablets|capsule|dose|dosage|mg|otc|over[- ]the[- ]counter|formulary|pharmac\w*|"
                    r"pain|ache|headache|fever|allerg\w*|hay fever|rhinitis|diarrh\w*|heartburn|cough|cold|flu|antihistamine|nsaid|ibuprofen|"
                    r"paracetamol|acetaminophen|aspirin|cetirizine|loratadine|omeprazole|ors|rehydration|warfarin|pregnan\w*|breastfeed\w*|"
                    r"side effects?|interaction|storage|store|expiry|batch|lot|sku|policy|sop|escalat\w*|prescription|symptom\w*|treat\w*|"
                    r"relief|remedy|drowsy|drowsiness|sneez\w*|itch\w*|nausea|vomit\w*|cancer|chemotherap\w*|insulin|antibiotic\w*|"
                    r"approval|approve|pending)\b", re.I)


@dataclass
class TriageOut:
    intent: str
    domain: str
    risk_tier: int
    complexity: str
    route: str
    reason: str = ""


def classify(q: str) -> TriageOut:
    if EMERGENCY.search(q):
        return TriageOut("emergency", "safety", 3, "low", "REJECT_OOS", "emergency")
    if LOT_RX.search(q) and TX_VERB.search(q):
        return TriageOut("lot_transition", "sop", 3, "low", "TRANSACTION_EXEC", "lot state change")
    if CLINICAL_ACTION.search(q):
        return TriageOut("clinical_action", "clinical", 3, "low", "REJECT_OOS", "clinical execution forbidden without licensed human")
    if GREETING.match(q):
        return TriageOut("greeting", "general", 1, "low", "DIRECT_ANSWER")
    if DOMAIN.search(q):
        words = len(q.split())
        return TriageOut("lookup", "formulary", 1, "high" if words > 25 else "low", "HYBRID_RAG")
    return TriageOut("oos", "none", 1, "low", "REJECT_OOS", "out of scope")
