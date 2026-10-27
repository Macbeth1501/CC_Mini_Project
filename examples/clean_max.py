def largest(items):
    best = None
    for x in items:
        if best is None:
            best = x
        elif x > best:
            best = x
    return best
