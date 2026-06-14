def calculate_average(total, count):
    # Bug: if count is 0, this will crash
    return total / count

if __name__ == "__main__":
    calculate_average(100, 0)
