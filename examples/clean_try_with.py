def read_number(path):
    try:
        with open(path) as fh:
            text = fh.read()
        value = int(text)
    except (OSError, ValueError):
        value = -1
    return value


def squares_of_odds(items):
    odds = [x * x for x in items if x % 2]
    total = sum(v for v in odds)
    scale = lambda k: k * total
    return scale(2), odds
