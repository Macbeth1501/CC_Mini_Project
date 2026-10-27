def classify(n):
    if n < 0:
        label = "neg"
    elif n == 0:
        label = "zero"
    else:
        label = "pos"
    return label
