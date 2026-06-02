from __future__ import annotations

def result_label(result: object) -> str:
    if getattr(result, "ok"):
        return "OK"
    if getattr(result, "blocks_startup", True):
        return "FAIL"
    return "WARN"


def result_name(result: object) -> str:
    return str(getattr(result, "service", getattr(result, "name", "")))


def print_results(results: list[object]) -> None:
    for result in results:
        print(f"{result_label(result)} {result_name(result)}: {getattr(result, 'message')}")
