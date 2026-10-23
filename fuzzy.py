"""Fuzzy matching for search suggestions: pure string scoring, no I/O."""


def _word(w, text):
    """Score one query word against lowercased text, or None if its letters don't appear in order."""
    i = text.find(w)
    if i >= 0:  # a plain substring beats scattered letters, more so at the start of a word
        return 100 + len(w) * 3 + (20 if i == 0 or not text[i - 1].isalnum() else 0)
    score, pos, prev = 0, 0, -2
    for ch in w:
        j = text.find(ch, pos)
        if j < 0:
            return None
        score += 1 + (5 if j == prev + 1 else 0) + (8 if j == 0 or not text[j - 1].isalnum() else 0)
        score -= min(j - pos, 5)  # a gap costs, but only so much
        prev, pos = j, j + 1
    return score


def score(query, text):
    """Higher is better; None when some word of query doesn't match. Shorter texts win ties."""
    words, text = query.lower().split(), (text or "").lower()
    if not words:
        return None
    total = 0
    for w in words:
        s = _word(w, text)
        if s is None:
            return None
        total += s
    return total - len(text) / 100
