def find_first_negative(items):
    count = 0
    for x in items:
        if x == 0:
            continue
        if x < 0:
            break
        count += 1
    i = 0
    while i < 10:
        i += 1
    return count
