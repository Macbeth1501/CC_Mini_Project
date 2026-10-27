def report(rows, verbose):
    total = 0
    for row in rows:
        if row > 0:
            while row > 10:
                if verbose:
                    note = "big row"
                row -= 10
                print(note)
        total += row
    return total
