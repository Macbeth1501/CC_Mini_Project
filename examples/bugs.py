def buggy(a, flag):
    unused = a * 2
    if flag:
        result = a + 1
    total = result + 1
    return total
    print("never")
