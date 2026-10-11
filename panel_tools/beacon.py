"""Morse light beacon timing. Pure functions so the panel and tests share them."""

MORSE = {'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.', 'G': '--.',
         'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..', 'M': '--', 'N': '-.',
         'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.', 'S': '...', 'T': '-', 'U': '..-',
         'V': '...-', 'W': '.--', 'X': '-..-', 'Y': '-.--', 'Z': '--..', '0': '-----',
         '1': '.----', '2': '..---', '3': '...--', '4': '....-', '5': '.....', '6': '-....',
         '7': '--...', '8': '---..', '9': '----.'}

# One Morse unit. A dot plus its gap is two units, so 0.3 s flashes at most ~1.7 times a
# second: readable by eye and below the 3 Hz where flashing starts to risk seizures.
UNIT = .3


def schedule(text, unit=UNIT):
    """[(lit, seconds), ...] for one pass of text, ending with a word gap so it can loop.
    Standard timing: dot 1, dash 3, gap inside a letter 1, between letters 3, words 7."""
    steps = []
    for word in text.upper().split():
        letters = [MORSE[c] for c in word if c in MORSE]
        for i, code in enumerate(letters):
            for j, mark in enumerate(code):
                steps.append((True, (1 if mark == '.' else 3)*unit))
                if j < len(code)-1:
                    steps.append((False, unit))
            if i < len(letters)-1:
                steps.append((False, 3*unit))
        steps.append((False, 7*unit))
    return steps


def state_at(steps, elapsed):
    """(lit, seconds until the next change) at a time since the beacon started, looping."""
    period = sum(seconds for _, seconds in steps)
    if period <= 0:
        return False, 1.
    t = elapsed % period
    for lit, seconds in steps:
        if t < seconds:
            return lit, seconds - t
        t -= seconds
    return steps[-1][0], steps[-1][1]
