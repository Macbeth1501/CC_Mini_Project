def in_range(x, lo, hi):
    if lo <= x and x <= hi or x == 0:
        return True
    return False


def first_match(items, wanted):
    i = 0
    while i < len(items) and items[i] != wanted:
        i += 1
    return i


def poll(n):
    while True:
        n += 1
        if n > 10:
            break
    return n
