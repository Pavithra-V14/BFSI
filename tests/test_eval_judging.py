def test_the_disclaimer_and_warnings_our_code_added_are_not_scored_as_claims():
    from app.eval.judging import strip_fixed_text
    from app.guardrails import messages

    core = "The rate is 12.5 per cent."
    assert strip_fixed_text(f"{core}\n\n{messages.DISCLAIMER}") == core
    warned = f"{core}\n\n{messages.WARNING_NUMBERS.format(figures='9.9')}\n\n{messages.DISCLAIMER}"
    assert strip_fixed_text(warned) == core
    assert strip_fixed_text(f"{core}\n\n{messages.WARNING_CITATIONS}") == core
    assert strip_fixed_text(core) == core  # nothing appended: unchanged
    assert strip_fixed_text(messages.DISCLAIMER) == messages.DISCLAIMER  # never an empty answer
