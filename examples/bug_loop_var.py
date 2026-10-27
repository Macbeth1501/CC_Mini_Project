def last_positive(items):
    count = 0
    for x in items:
        if x > 0:
            last = x
        count += 1
    return last
