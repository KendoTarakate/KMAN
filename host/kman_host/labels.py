"""Activity labels (keyboard digit -> label). Stored in every session file so
analysis never depends on this table staying unchanged."""

LABELS: dict[int, str] = {
    0: "none",
    1: "rest breathing",
    2: "talking",
    3: "drinking/swallow",
    4: "dry swallow",
    5: "head movement",
    6: "walking",
    7: "cough",
    8: "breath-hold",
    9: "simulated obstruction",
}


def label_name(code: int) -> str:
    return LABELS.get(int(code), f"label {code}")


def help_text() -> str:
    return "  ".join(f"{k}={v}" for k, v in LABELS.items())
