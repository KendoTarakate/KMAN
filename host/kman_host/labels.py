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


# Label colours: categorical slots 1-8 of the validated reference palette in
# fixed order (labels 1-8). There is no 9th hue: label 9 is neutral gray and is
# drawn with a hatch, and every label band also carries its name as text.
LABEL_COLORS_LIGHT: dict[int, str] = {
    1: "#2a78d6", 2: "#eb6834", 3: "#1baf7a", 4: "#eda100",
    5: "#e87ba4", 6: "#008300", 7: "#4a3aa7", 8: "#e34948", 9: "#8a8984",
}
LABEL_COLORS_DARK: dict[int, str] = {
    1: "#3987e5", 2: "#d95926", 3: "#199e70", 4: "#c98500",
    5: "#d55181", 6: "#008300", 7: "#9085e9", 8: "#e66767", 9: "#8a8984",
}
HATCHED_LABELS = {9}
